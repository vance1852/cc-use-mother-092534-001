"""急诊节日值守与家属联络领域服务。

业务链：岗位资格 -> 班次版本 -> 交班事项/临时换岗 -> 患者联络授权
-> 通知窗口投递 -> 失败人工处置。所有状态落 SQLite，写操作复用基础层
request_receipts 幂等回执与哈希审计，进程重启后调度器继续处理未决事项。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from festival_foundation.audit import append_event, canonical_json, digest
from festival_foundation.clock import Clock, SystemClock
from festival_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from festival_foundation.storage import Database

from .errors import DeliveryConflictError, HandoverBlockedError
from .models import (
    Assignment,
    ContactAuthorization,
    DutySnapshot,
    Handover,
    HandoverItem,
    ManualTask,
    NotificationRequest,
    Patient,
    Qualification,
    Shift,
)
from .schema import SCHEMA
from .timeutil import (
    add_minutes,
    format_utc,
    next_window_open,
    parse_utc,
    shift_local_date,
    within_window,
)
from .transports import NotificationTransport, RecordingTransport

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

CARE_STATES = frozenset({"observation", "rescue", "discharged"})
WRITE_ROLES = frozenset({"admin", "operator"})
NOTIFY_ROLES = frozenset({"admin", "operator", "reviewer"})

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (60, 300, 900)
DEFAULT_TTL_MINUTES = 1440
MANUAL_SLA_MINUTES = 120
SCOPE_VALUES = frozenset({"condition", "location", "plan", "administrative"})
ITEM_KINDS = frozenset({"rescue", "observation", "cross_support", "followup"})


class EmergencyDutyService:
    """协调资格、班次、换岗、授权、通知与人工处置规则。"""

    def __init__(self, database: Database, clock: Clock | None = None,
                 transport: NotificationTransport | None = None,
                 max_attempts: int = MAX_ATTEMPTS,
                 manual_sla_minutes: int = MANUAL_SLA_MINUTES) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        self.transport = transport or RecordingTransport()
        self.max_attempts = max_attempts
        self.manual_sla_minutes = manual_sla_minutes
        database.connection.executescript(SCHEMA)

    # ------------------------------------------------------------------ 基础

    def _now(self) -> datetime:
        return self.clock.now()

    def _now_str(self) -> str:
        return format_utc(self._now())

    def _id(self, value: str, field: str) -> str:
        value = str(value or "").strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 500) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require_roles(self, actor, roles: Iterable[str]) -> None:
        if actor["role"] not in frozenset(roles):
            raise PermissionDenied("当前角色不能执行该动作")

    def _site(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("院区不存在")
        return row

    def _same_org(self, actor, site_row) -> None:
        if actor["role"] != "admin" and actor["organization_id"] != site_row["organization_id"]:
            raise PermissionDenied("不能操作其他组织的院区")

    def _idempotent(self, connection, *, request_id: str, action: str, payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]],
                    conflict_error: type[ConflictError] = ConflictError) -> dict[str, Any]:
        """复用基础层 request_receipts：相同内容安全重放，不同内容冲突。

        conflict_error 可让调用方区分冲突语义（例如通知内容冲突）。
        """

        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise conflict_error("request_id 已被不同内容使用")
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True,
                    **json.loads(row["response_json"])}

        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now_str()),
        )
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False, **response}

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now_str())

    # ----------------------------------------------------------- 患者与资格

    def register_patient(self, *, request_id: str, actor_id: str, patient_id: str,
                         site_id: str, display_name: str,
                         care_state: str = "observation") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "patient_id": patient_id, "site_id": site_id,
                   "display_name": display_name, "care_state": care_state}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, WRITE_ROLES)
            site = self._site(conn, site_id)
            self._same_org(actor, site)
            patient_id = self._id(patient_id, "patient_id")
            display_name = self._text(display_name, "display_name", 100)
            if care_state not in CARE_STATES:
                raise ValidationError("care_state 不合法")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO ed_patients(patient_id,site_id,display_name,care_state,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?)",
                        (patient_id, site_id, display_name, care_state, actor_id, self._now_str()),
                    )
                except Exception as exc:
                    raise ConflictError("患者编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="ed.patient.registered",
                            resource_type="ed_patient", resource_id=patient_id,
                            detail={"site_id": site_id, "care_state": care_state})
                return "ed_patient", patient_id, {"patient_id": patient_id, "status": "registered"}

            return self._idempotent(conn, request_id=request_id, action="ed.register_patient",
                                    payload=payload, create=create)

    def update_patient_state(self, *, request_id: str, actor_id: str, patient_id: str,
                             care_state: str) -> dict[str, Any]:
        if care_state not in CARE_STATES:
            raise ValidationError("care_state 不合法")
        payload = {"actor_id": actor_id, "patient_id": patient_id, "care_state": care_state}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, WRITE_ROLES)
            patient = self._patient(conn, patient_id)
            self._same_org(actor, self._site(conn, patient["site_id"]))

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute("UPDATE ed_patients SET care_state=? WHERE patient_id=?",
                             (care_state, patient_id))
                self._audit(conn, actor_id=actor_id, action="ed.patient.state_changed",
                            resource_type="ed_patient", resource_id=patient_id,
                            detail={"care_state": care_state})
                return "ed_patient", patient_id, {"patient_id": patient_id, "status": care_state}

            return self._idempotent(conn, request_id=request_id, action="ed.update_patient_state",
                                    payload=payload, create=create)

    def grant_qualification(self, *, request_id: str, actor_id: str, qualification_id: str,
                            site_id: str, target_actor_id: str, position_code: str,
                            valid_from: str, valid_until: str) -> dict[str, Any]:
        start = parse_utc(valid_from)
        end = parse_utc(valid_until)
        if end <= start:
            raise ValidationError("资格有效期结束时间必须晚于开始时间")
        payload = {"actor_id": actor_id, "qualification_id": qualification_id, "site_id": site_id,
                   "target_actor_id": target_actor_id, "position_code": position_code,
                   "valid_from": format_utc(start), "valid_until": format_utc(end)}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, WRITE_ROLES)
            site = self._site(conn, site_id)
            self._same_org(actor, site)
            target = self._actor(conn, target_actor_id)
            if target["organization_id"] != site["organization_id"] and actor["role"] != "admin":
                raise PermissionDenied("不能为其他组织的操作者登记岗位资格")
            qualification_id = self._id(qualification_id, "qualification_id")
            position_code = self._id(position_code, "position_code")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO ed_qualifications(qualification_id,site_id,actor_id,"
                        "position_code,valid_from,valid_until,revoked,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,0,?,?)",
                        (qualification_id, site_id, target_actor_id, position_code,
                         format_utc(start), format_utc(end), actor_id, self._now_str()),
                    )
                except Exception as exc:
                    raise ConflictError("资格编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="ed.qualification.granted",
                            resource_type="ed_qualification", resource_id=qualification_id,
                            detail={"site_id": site_id, "target_actor_id": target_actor_id,
                                    "position_code": position_code})
                return "ed_qualification", qualification_id, {"status": "granted"}

            return self._idempotent(conn, request_id=request_id, action="ed.grant_qualification",
                                    payload=payload, create=create)

    def revoke_qualification(self, *, request_id: str, actor_id: str,
                             qualification_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "qualification_id": qualification_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, WRITE_ROLES)
            row = conn.execute("SELECT * FROM ed_qualifications WHERE qualification_id=?",
                               (qualification_id,)).fetchone()
            if row is None:
                raise NotFoundError("岗位资格不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute("UPDATE ed_qualifications SET revoked=1 WHERE qualification_id=?",
                             (qualification_id,))
                self._audit(conn, actor_id=actor_id, action="ed.qualification.revoked",
                            resource_type="ed_qualification", resource_id=qualification_id,
                            detail={"site_id": row["site_id"], "actor_id": row["actor_id"],
                                    "position_code": row["position_code"]})
                return "ed_qualification", qualification_id, {"status": "revoked"}

            return self._idempotent(conn, request_id=request_id,
                                    action="ed.revoke_qualification", payload=payload, create=create)

    def _qualified(self, conn, *, site_id: str, actor_id: str, position_code: str,
                   moment: datetime) -> bool:
        row = conn.execute(
            "SELECT 1 FROM ed_qualifications WHERE site_id=? AND actor_id=? AND position_code=? "
            "AND revoked=0 AND valid_from<=? AND valid_until>=?",
            (site_id, actor_id, position_code, format_utc(moment), format_utc(moment)),
        ).fetchone()
        return row is not None

    def list_qualifications(self, site_id: str, actor_id: str | None = None) -> list[Qualification]:
        query = "SELECT * FROM ed_qualifications WHERE site_id=?"
        params: list[Any] = [site_id]
        if actor_id:
            query += " AND actor_id=?"
            params.append(actor_id)
        query += " ORDER BY valid_from"
        rows = self.database.connection.execute(query, params).fetchall()
        return [Qualification(r["qualification_id"], r["site_id"], r["actor_id"],
                              r["position_code"], r["valid_from"], r["valid_until"],
                              bool(r["revoked"])) for r in rows]

    # --------------------------------------------------------------- 班次版本

    def publish_shift(self, *, request_id: str, actor_id: str, shift_id: str, site_id: str,
                      starts_at: str, ends_at: str,
                      assignments: list[dict[str, str]]) -> dict[str, Any]:
        start = parse_utc(starts_at)
        end = parse_utc(ends_at)
        if end <= start:
            raise ValidationError("班次结束时间必须晚于开始时间")
        if not assignments:
            raise ValidationError("班次至少包含一个岗位")
        normalized: list[dict[str, str]] = []
        for entry in assignments:
            position_code = self._id(entry["position_code"], "position_code")
            holder = self._id(entry["holder_actor_id"], "holder_actor_id")
            normalized.append({"position_code": position_code, "holder_actor_id": holder})
        positions = [e["position_code"] for e in normalized]
        if len(set(positions)) != len(positions):
            raise ValidationError("同一班次内岗位不能重复")
        payload = {"actor_id": actor_id, "shift_id": shift_id, "site_id": site_id,
                   "starts_at": format_utc(start), "ends_at": format_utc(end),
                   "assignments": normalized}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, WRITE_ROLES)
            site = self._site(conn, site_id)
            self._same_org(actor, site)
            shift_id = self._id(shift_id, "shift_id")
            date = shift_local_date(start, end, site["timezone_name"])
            version = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS v FROM ed_shifts WHERE site_id=? AND shift_date=?",
                (site_id, date),
            ).fetchone()["v"]
            for entry in normalized:
                self._actor(conn, entry["holder_actor_id"])
                if not self._qualified(conn, site_id=site_id, actor_id=entry["holder_actor_id"],
                                       position_code=entry["position_code"], moment=start) or not \
                        self._qualified(conn, site_id=site_id, actor_id=entry["holder_actor_id"],
                                        position_code=entry["position_code"], moment=end):
                    raise HandoverBlockedError(
                        f"{entry['holder_actor_id']} 缺少岗位 {entry['position_code']} 的有效资格"
                    )

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "UPDATE ed_shifts SET active=0, superseded_by=? WHERE site_id=? AND shift_date=? AND active=1",
                    (shift_id, site_id, date),
                )
                try:
                    conn.execute(
                        "INSERT INTO ed_shifts(shift_id,site_id,shift_date,version,starts_at,ends_at,"
                        "local_date,active,created_by,created_at) VALUES(?,?,?,?,?,?,?,1,?,?)",
                        (shift_id, site_id, date, version, format_utc(start), format_utc(end),
                         date, actor_id, self._now_str()),
                    )
                except Exception as exc:
                    raise ConflictError("班次编号已经存在") from exc
                for index, entry in enumerate(normalized, start=1):
                    conn.execute(
                        "INSERT INTO ed_shift_assignments(assignment_id,shift_id,site_id,"
                        "position_code,holder_actor_id,sequence_no) VALUES(?,?,?,?,?,?)",
                        (uuid.uuid4().hex, shift_id, site_id, entry["position_code"],
                         entry["holder_actor_id"], index),
                    )
                self._audit(conn, actor_id=actor_id, action="ed.shift.published",
                            resource_type="ed_shift", resource_id=shift_id,
                            detail={"site_id": site_id, "shift_date": date, "version": version,
                                    "assignments": normalized})
                return "ed_shift", shift_id, {"status": "active", "version": version,
                                              "shift_date": date}

            return self._idempotent(conn, request_id=request_id, action="ed.publish_shift",
                                    payload=payload, create=create)

    def get_shift(self, shift_id: str) -> Shift:
        row = self.database.connection.execute(
            "SELECT * FROM ed_shifts WHERE shift_id=?", (shift_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("班次不存在")
        assignments = tuple(self._assignments(row["shift_id"]))
        return Shift(row["shift_id"], row["site_id"], row["shift_date"], row["version"],
                     row["starts_at"], row["ends_at"], row["local_date"], bool(row["active"]),
                     assignments)

    def _assignments(self, shift_id: str) -> list[Assignment]:
        rows = self.database.connection.execute(
            "SELECT * FROM ed_shift_assignments WHERE shift_id=? ORDER BY sequence_no", (shift_id,)
        ).fetchall()
        return [Assignment(r["assignment_id"], r["shift_id"], r["position_code"],
                           r["holder_actor_id"], r["sequence_no"]) for r in rows]

    def _current_shift_row(self, conn, site_id: str, moment: datetime):
        stamp = format_utc(moment)
        return conn.execute(
            "SELECT * FROM ed_shifts WHERE site_id=? AND active=1 AND starts_at<=? AND ends_at>? "
            "ORDER BY version DESC LIMIT 1",
            (site_id, stamp, stamp),
        ).fetchone()

    # ------------------------------------------------------------- 交班事项

    def _patient(self, conn, patient_id: str):
        row = conn.execute("SELECT * FROM ed_patients WHERE patient_id=?", (patient_id,)).fetchone()
        if row is None:
            raise NotFoundError("患者不存在")
        return row

    def add_handover_item(self, *, request_id: str, actor_id: str, shift_id: str,
                          position_code: str, kind: str, summary: str,
                          patient_id: str | None = None, critical: bool = False) -> dict[str, Any]:
        if kind not in ITEM_KINDS:
            raise ValidationError("事项类型不合法")
        payload = {"actor_id": actor_id, "shift_id": shift_id, "position_code": position_code,
                   "kind": kind, "summary": summary, "patient_id": patient_id,
                   "critical": bool(critical)}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, WRITE_ROLES)
            shift = conn.execute("SELECT * FROM ed_shifts WHERE shift_id=?", (shift_id,)).fetchone()
            if shift is None:
                raise NotFoundError("班次不存在")
            self._same_org(actor, self._site(conn, shift["site_id"]))
            if not shift["active"]:
                raise ConflictError("班次版本已停用，不能再登记事项")
            assignment = conn.execute(
                "SELECT * FROM ed_shift_assignments WHERE shift_id=? AND position_code=?",
                (shift_id, position_code),
            ).fetchone()
            if assignment is None:
                raise NotFoundError("该班次上没有这个岗位")
            if patient_id:
                patient = self._patient(conn, patient_id)
                if patient["site_id"] != shift["site_id"]:
                    raise ValidationError("患者与班次不属于同一院区")
            item_id = uuid.uuid4().hex
            summary_text = self._text(summary, "summary", 1000)

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "INSERT INTO ed_handover_items(item_id,shift_id,site_id,patient_id,"
                    "position_code,kind,summary,critical,status,assignment_id,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,'open',?,?,?)",
                    (item_id, shift_id, shift["site_id"], patient_id, position_code, kind,
                     summary_text, 1 if critical else 0, assignment["assignment_id"],
                     actor_id, self._now_str()),
                )
                self._audit(conn, actor_id=actor_id, action="ed.handover_item.added",
                            resource_type="ed_handover_item", resource_id=item_id,
                            detail={"shift_id": shift_id, "position_code": position_code,
                                    "patient_id": patient_id, "critical": bool(critical),
                                    "kind": kind})
                return "ed_handover_item", item_id, {"item_id": item_id, "status": "open"}

            return self._idempotent(conn, request_id=request_id, action="ed.add_handover_item",
                                    payload=payload, create=create)

    def complete_handover_item(self, *, request_id: str, actor_id: str, item_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "item_id": item_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, WRITE_ROLES)
            item = self._item(conn, item_id)
            self._same_org(actor, self._site(conn, item["site_id"]))

            def create() -> tuple[str, str, dict[str, Any]]:
                if item["status"] != "open":
                    raise ConflictError(f"事项当前状态为 {item['status']}，不能完成")
                conn.execute(
                    "UPDATE ed_handover_items SET status='completed', completed_at=? WHERE item_id=?",
                    (self._now_str(), item_id),
                )
                self._audit(conn, actor_id=actor_id, action="ed.handover_item.completed",
                            resource_type="ed_handover_item", resource_id=item_id,
                            detail={"shift_id": item["shift_id"]})
                return "ed_handover_item", item_id, {"item_id": item_id, "status": "completed"}

            return self._idempotent(conn, request_id=request_id,
                                    action="ed.complete_handover_item", payload=payload, create=create)

    def transfer_handover_item(self, *, request_id: str, actor_id: str, item_id: str,
                               to_shift_id: str, to_position_code: str) -> dict[str, Any]:
        """跨科/跨岗位支援转移：抢救中的关键事项禁止转移。"""

        payload = {"actor_id": actor_id, "item_id": item_id, "to_shift_id": to_shift_id,
                   "to_position_code": to_position_code}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, WRITE_ROLES)
            item = self._item(conn, item_id)
            self._same_org(actor, self._site(conn, item["site_id"]))
            target = conn.execute(
                "SELECT * FROM ed_shift_assignments WHERE shift_id=? AND position_code=?",
                (to_shift_id, to_position_code),
            ).fetchone()
            if target is None:
                raise NotFoundError("目标班次上没有这个岗位")
            if item["status"] != "open":
                raise ConflictError("只有未结办的事项可以转移")
            blocked = self._blocking_critical_items(conn, item["assignment_id"])
            if any(b["item_id"] == item_id for b in blocked):
                raise HandoverBlockedError("抢救中的关键事项不得转移")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "UPDATE ed_handover_items SET status='transferred', assignment_id=?, "
                    "position_code=?, transferred_to=?, transferred_at=? WHERE item_id=?",
                    (target["assignment_id"], to_position_code, target["assignment_id"],
                     self._now_str(), item_id),
                )
                self._audit(conn, actor_id=actor_id, action="ed.handover_item.transferred",
                            resource_type="ed_handover_item", resource_id=item_id,
                            detail={"from_position": item["position_code"],
                                    "to_position": to_position_code, "to_shift_id": to_shift_id})
                return "ed_handover_item", item_id, {"item_id": item_id, "status": "transferred"}

            return self._idempotent(conn, request_id=request_id,
                                    action="ed.transfer_handover_item", payload=payload, create=create)

    def _item(self, conn, item_id: str):
        row = conn.execute("SELECT * FROM ed_handover_items WHERE item_id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("交班事项不存在")
        return row

    def list_handover_items(self, shift_id: str, status: str | None = None) -> list[HandoverItem]:
        query = "SELECT * FROM ed_handover_items WHERE shift_id=?"
        params: list[Any] = [shift_id]
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY created_at, item_id"
        rows = self.database.connection.execute(query, params).fetchall()
        return [self._item_model(r) for r in rows]

    def _item_model(self, r) -> HandoverItem:
        return HandoverItem(r["item_id"], r["shift_id"], r["site_id"], r["patient_id"],
                            r["position_code"], r["kind"], r["summary"], bool(r["critical"]),
                            r["status"], r["assignment_id"], r["transferred_to"],
                            r["transferred_at"], r["completed_at"], r["created_by"], r["created_at"])

    # ----------------------------------------------------------- 临时换岗链

    def propose_handover(self, *, request_id: str, actor_id: str, shift_id: str,
                         position_code: str, incoming_actor_id: str,
                         reason: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "shift_id": shift_id, "position_code": position_code,
                   "incoming_actor_id": incoming_actor_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            shift = conn.execute("SELECT * FROM ed_shifts WHERE shift_id=?", (shift_id,)).fetchone()
            if shift is None:
                raise NotFoundError("班次不存在")
            site = self._site(conn, shift["site_id"])
            self._same_org(actor, site)
            if not shift["active"]:
                raise ConflictError("班次版本已停用")
            assignment = conn.execute(
                "SELECT * FROM ed_shift_assignments WHERE shift_id=? AND position_code=?",
                (shift_id, position_code),
            ).fetchone()
            if assignment is None:
                raise NotFoundError("该班次上没有这个岗位")
            incoming = self._actor(conn, incoming_actor_id)
            outgoing_actor_id = assignment["holder_actor_id"]
            if incoming_actor_id == outgoing_actor_id:
                raise ValidationError("接任者与当前责任人相同")
            if actor_id not in {outgoing_actor_id, incoming_actor_id}:
                self._require_roles(actor, WRITE_ROLES)
            handover_id = uuid.uuid4().hex
            if reason is not None:
                reason = self._text(reason, "reason", 300)

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "INSERT INTO ed_handovers(handover_id,site_id,shift_id,assignment_id,"
                    "position_code,outgoing_actor_id,incoming_actor_id,status,reason,"
                    "outgoing_confirmed,incoming_confirmed,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,'proposed',?,?,?,?,?)",
                    (handover_id, shift["site_id"], shift_id, assignment["assignment_id"],
                     position_code, outgoing_actor_id, incoming_actor_id, reason,
                     1 if actor_id == outgoing_actor_id else 0,
                     1 if actor_id == incoming_actor_id else 0,
                     actor_id, self._now_str()),
                )
                self._audit(conn, actor_id=actor_id, action="ed.handover.proposed",
                            resource_type="ed_handover", resource_id=handover_id,
                            detail={"shift_id": shift_id, "position_code": position_code,
                                    "outgoing_actor_id": outgoing_actor_id,
                                    "incoming_actor_id": incoming_actor_id, "reason": reason})
                # 资格在双方确认、换岗生效时才做硬性校验；提议阶段只记录决定。
                return "ed_handover", handover_id, {"handover_id": handover_id, "status": "proposed",
                                                    "blocked_reason": None}

            return self._idempotent(conn, request_id=request_id, action="ed.propose_handover",
                                    payload=payload, create=create)

    def confirm_handover(self, *, request_id: str, actor_id: str, handover_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "handover_id": handover_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            handover = self._handover(conn, handover_id)
            self._same_org(actor, self._site(conn, handover["site_id"]))
            if handover["status"] != "proposed":
                raise ConflictError(f"换岗当前状态为 {handover['status']}")
            if actor_id == handover["outgoing_actor_id"]:
                column = "outgoing_confirmed"
            elif actor_id == handover["incoming_actor_id"]:
                column = "incoming_confirmed"
            else:
                raise PermissionDenied("只有交接双方可以确认换岗")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(f"UPDATE ed_handovers SET {column}=1 WHERE handover_id=?",
                             (handover_id,))
                self._audit(conn, actor_id=actor_id, action="ed.handover.confirmed",
                            resource_type="ed_handover", resource_id=handover_id,
                            detail={"party": column.replace("_confirmed", "")})
                row = conn.execute("SELECT * FROM ed_handovers WHERE handover_id=?",
                                   (handover_id,)).fetchone()
                status, reason = self._try_effectuate(conn, row)
                return "ed_handover", handover_id, {"handover_id": handover_id, "status": status,
                                                    "blocked_reason": reason}

            return self._idempotent(conn, request_id=request_id, action="ed.confirm_handover",
                                    payload=payload, create=create)

    def reject_handover(self, *, request_id: str, actor_id: str, handover_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "handover_id": handover_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            handover = self._handover(conn, handover_id)
            if actor_id not in (handover["outgoing_actor_id"], handover["incoming_actor_id"]):
                self._require_roles(actor, WRITE_ROLES)
            if handover["status"] not in ("proposed",):
                raise ConflictError(f"换岗当前状态为 {handover['status']}")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute("UPDATE ed_handovers SET status='rejected' WHERE handover_id=?",
                             (handover_id,))
                self._audit(conn, actor_id=actor_id, action="ed.handover.rejected",
                            resource_type="ed_handover", resource_id=handover_id, detail={})
                return "ed_handover", handover_id, {"handover_id": handover_id,
                                                    "status": "rejected", "blocked_reason": None}

            return self._idempotent(conn, request_id=request_id, action="ed.reject_handover",
                                    payload=payload, create=create)

    def retry_handover(self, *, request_id: str, actor_id: str, handover_id: str) -> dict[str, Any]:
        """解除阻塞条件后（资格补办、抢救结束）重新尝试生效。"""

        payload = {"actor_id": actor_id, "handover_id": handover_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            handover = self._handover(conn, handover_id)
            self._same_org(actor, self._site(conn, handover["site_id"]))
            if handover["status"] != "proposed":
                raise ConflictError(f"换岗当前状态为 {handover['status']}，不能重试")
            if not (handover["outgoing_confirmed"] and handover["incoming_confirmed"]):
                raise ConflictError("双方尚未完成确认")

            def create() -> tuple[str, str, dict[str, Any]]:
                status, reason = self._try_effectuate(conn, handover)
                return "ed_handover", handover_id, {"handover_id": handover_id, "status": status,
                                                    "blocked_reason": reason}

            return self._idempotent(conn, request_id=request_id, action="ed.retry_handover",
                                    payload=payload, create=create)

    def _blocking_critical_items(self, conn, assignment_id: str):
        """关键事项且患者处于抢救中：阻塞交接，绝不自动转移。"""

        return conn.execute(
            "SELECT i.* FROM ed_handover_items i JOIN ed_patients p ON p.patient_id=i.patient_id "
            "WHERE i.assignment_id=? AND i.status='open' AND i.critical=1 AND p.care_state='rescue'",
            (assignment_id,),
        ).fetchall()

    def _try_effectuate(self, conn, handover) -> tuple[str, str | None]:
        now = self._now()
        if not self._qualified(conn, site_id=handover["site_id"],
                               actor_id=handover["incoming_actor_id"],
                               position_code=handover["position_code"], moment=now):
            reason = "qualification_invalid"
        elif self._blocking_critical_items(conn, handover["assignment_id"]):
            reason = "critical_rescue_items_open"
        else:
            reason = None
        if reason:
            conn.execute("UPDATE ed_handovers SET blocked_reason=? WHERE handover_id=?",
                         (reason, handover["handover_id"]))
            self._audit(conn, actor_id=handover["incoming_actor_id"],
                        action="ed.handover.blocked", resource_type="ed_handover",
                        resource_id=handover["handover_id"], detail={"reason": reason})
            return "blocked", reason
        conn.execute(
            "UPDATE ed_shift_assignments SET holder_actor_id=? WHERE assignment_id=?",
            (handover["incoming_actor_id"], handover["assignment_id"]),
        )
        conn.execute(
            "UPDATE ed_handovers SET status='effective', effective_at=?, blocked_reason=NULL "
            "WHERE handover_id=?",
            (format_utc(now), handover["handover_id"]),
        )
        self._audit(conn, actor_id=handover["incoming_actor_id"],
                    action="ed.handover.effective", resource_type="ed_handover",
                    resource_id=handover["handover_id"],
                    detail={"assignment_id": handover["assignment_id"],
                            "incoming_actor_id": handover["incoming_actor_id"]})
        return "effective", None

    def _handover(self, conn, handover_id: str):
        row = conn.execute("SELECT * FROM ed_handovers WHERE handover_id=?",
                           (handover_id,)).fetchone()
        if row is None:
            raise NotFoundError("换岗记录不存在")
        return row

    def get_handover(self, handover_id: str) -> Handover:
        r = self._handover(self.database.connection, handover_id)
        return Handover(r["handover_id"], r["site_id"], r["shift_id"], r["assignment_id"],
                        r["position_code"], r["outgoing_actor_id"], r["incoming_actor_id"],
                        r["status"], r["reason"], bool(r["outgoing_confirmed"]),
                        bool(r["incoming_confirmed"]), r["effective_at"], r["blocked_reason"],
                        r["created_by"], r["created_at"])

    def list_handovers(self, site_id: str, status: str | None = None) -> list[Handover]:
        query = "SELECT * FROM ed_handovers WHERE site_id=?"
        params: list[Any] = [site_id]
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY created_at, handover_id"
        rows = self.database.connection.execute(query, params).fetchall()
        return [Handover(r["handover_id"], r["site_id"], r["shift_id"], r["assignment_id"],
                         r["position_code"], r["outgoing_actor_id"], r["incoming_actor_id"],
                         r["status"], r["reason"], bool(r["outgoing_confirmed"]),
                         bool(r["incoming_confirmed"]), r["effective_at"], r["blocked_reason"],
                         r["created_by"], r["created_at"]) for r in rows]

    # ------------------------------------------------------------- 联络授权

    def grant_authorization(self, *, request_id: str, actor_id: str, patient_id: str,
                            contact_name: str, contact_channel: str, scope: list[str],
                            window_start: str | None = None,
                            window_end: str | None = None) -> dict[str, Any]:
        scope = list(scope or [])
        if not scope or any(s not in SCOPE_VALUES for s in scope):
            raise ValidationError("授权范围不合法")
        if (window_start is None) != (window_end is None):
            raise ValidationError("通知窗口必须同时给出开始与结束")
        if window_start:
            for value in (window_start, window_end):
                datetime.strptime(value, "%H:%M")  # 校验 HH:MM
        payload = {"actor_id": actor_id, "patient_id": patient_id, "contact_name": contact_name,
                   "contact_channel": contact_channel, "scope": sorted(scope),
                   "window_start": window_start, "window_end": window_end}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, WRITE_ROLES)
            patient = self._patient(conn, patient_id)
            self._same_org(actor, self._site(conn, patient["site_id"]))
            contact_name = self._text(contact_name, "contact_name", 100)
            contact_channel = self._id(contact_channel, "contact_channel")
            authorization_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "INSERT INTO ed_contact_authorizations(authorization_id,patient_id,site_id,"
                    "contact_name,contact_channel,scope_json,window_start,window_end,granted_by,"
                    "granted_at,revoked) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                    (authorization_id, patient_id, patient["site_id"], contact_name,
                     contact_channel, canonical_json(sorted(scope)), window_start, window_end,
                     actor_id, self._now_str()),
                )
                self._audit(conn, actor_id=actor_id, action="ed.authorization.granted",
                            resource_type="ed_authorization", resource_id=authorization_id,
                            detail={"patient_id": patient_id, "contact_name": contact_name,
                                    "scope": sorted(scope)})
                return "ed_authorization", authorization_id, {"authorization_id": authorization_id,
                                                              "status": "active"}

            return self._idempotent(conn, request_id=request_id, action="ed.grant_authorization",
                                    payload=payload, create=create)

    def revoke_authorization(self, *, request_id: str, actor_id: str,
                             authorization_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "authorization_id": authorization_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, WRITE_ROLES)
            row = self._authorization(conn, authorization_id)
            self._same_org(actor, self._site(conn, row["site_id"]))

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "UPDATE ed_contact_authorizations SET revoked=1, revoked_by=?, revoked_at=? "
                    "WHERE authorization_id=?",
                    (actor_id, self._now_str(), authorization_id),
                )
                # 立即阻断该授权下所有尚未发出的通知（含窗口等待、退避重试），
                # 不等调度周期；已投递的历史披露保持不变。
                pending = conn.execute(
                    "SELECT nr.request_id AS request_id FROM ed_notification_requests nr "
                    "JOIN ed_notification_deliveries nd ON nd.request_id=nr.request_id "
                    "WHERE nr.authorization_id=? AND nr.status='pending' "
                    "AND nd.status IN ('pending','retry')",
                    (authorization_id,),
                ).fetchall()
                for pending_row in pending:
                    conn.execute(
                        "UPDATE ed_notification_deliveries SET status='blocked', "
                        "detail='authorization_revoked', attempted_at=?, next_attempt_at=NULL "
                        "WHERE request_id=?",
                        (self._now_str(), pending_row["request_id"]),
                    )
                    conn.execute(
                        "UPDATE ed_notification_requests SET status='blocked' WHERE request_id=?",
                        (pending_row["request_id"],),
                    )
                    self._audit(conn, actor_id=actor_id, action="ed.notification.blocked",
                                resource_type="ed_notification",
                                resource_id=pending_row["request_id"],
                                detail={"reason": "authorization_revoked"})
                self._audit(conn, actor_id=actor_id, action="ed.authorization.revoked",
                            resource_type="ed_authorization", resource_id=authorization_id,
                            detail={"patient_id": row["patient_id"],
                                    "blocked_requests": len(pending)})
                return "ed_authorization", authorization_id, {
                    "authorization_id": authorization_id, "status": "revoked",
                    "blocked_requests": len(pending)}

            return self._idempotent(conn, request_id=request_id, action="ed.revoke_authorization",
                                    payload=payload, create=create)

    def _authorization(self, conn, authorization_id: str):
        row = conn.execute("SELECT * FROM ed_contact_authorizations WHERE authorization_id=?",
                           (authorization_id,)).fetchone()
        if row is None:
            raise NotFoundError("联络授权不存在")
        return row

    def get_authorization(self, authorization_id: str) -> ContactAuthorization:
        return self._authorization_model(
            self._authorization(self.database.connection, authorization_id))

    def list_authorizations(self, patient_id: str, include_revoked: bool = True) -> list[ContactAuthorization]:
        query = "SELECT * FROM ed_contact_authorizations WHERE patient_id=?"
        if not include_revoked:
            query += " AND revoked=0"
        rows = self.database.connection.execute(query, (patient_id,)).fetchall()
        return [self._authorization_model(r) for r in rows]

    def _authorization_model(self, r) -> ContactAuthorization:
        return ContactAuthorization(r["authorization_id"], r["patient_id"], r["site_id"],
                                    r["contact_name"], r["contact_channel"],
                                    tuple(json.loads(r["scope_json"])), r["window_start"],
                                    r["window_end"], bool(r["revoked"]), r["granted_by"],
                                    r["granted_at"], r["revoked_by"], r["revoked_at"])

    def log_disclosure(self, *, request_id: str, actor_id: str, authorization_id: str,
                       summary: str) -> dict[str, Any]:
        """登记一次人工披露（电话/口头）。撤回立即阻止，但历史行保留。"""

        payload = {"actor_id": actor_id, "authorization_id": authorization_id, "summary": summary}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, NOTIFY_ROLES)
            auth = self._authorization(conn, authorization_id)
            self._same_org(actor, self._site(conn, auth["site_id"]))
            if auth["revoked"]:
                raise PermissionDenied("联络授权已撤回，禁止继续披露")
            now = self._now()
            site = self._site(conn, auth["site_id"])
            local_now = now.astimezone(ZoneInfo(site["timezone_name"]))
            if not within_window(local_now, auth["window_start"], auth["window_end"]):
                raise PermissionDenied("当前不在授权通知窗口内")
            summary_text = self._text(summary, "summary", 1000)

            def create() -> tuple[str, str, dict[str, Any]]:
                access_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO ed_authorization_access(access_id,authorization_id,"
                    "notification_request_id,summary,disclosed_by,disclosed_at) "
                    "VALUES(?,?,NULL,?,?,?)",
                    (access_id, authorization_id, summary_text, actor_id, format_utc(now)),
                )
                self._audit(conn, actor_id=actor_id, action="ed.disclosure.logged",
                            resource_type="ed_authorization", resource_id=authorization_id,
                            detail={"access_id": access_id})
                return "ed_authorization_access", access_id, {"access_id": access_id,
                                                              "status": "recorded"}

            return self._idempotent(conn, request_id=request_id, action="ed.log_disclosure",
                                    payload=payload, create=create)

    def list_access_history(self, authorization_id: str) -> list[dict[str, Any]]:
        """授权撤回后历史访问仍然可查。"""

        rows = self.database.connection.execute(
            "SELECT * FROM ed_authorization_access WHERE authorization_id=? "
            "ORDER BY disclosed_at, access_id",
            (authorization_id,),
        ).fetchall()
        return [{"access_id": r["access_id"], "authorization_id": r["authorization_id"],
                 "notification_request_id": r["notification_request_id"], "summary": r["summary"],
                 "disclosed_by": r["disclosed_by"], "disclosed_at": r["disclosed_at"]} for r in rows]

    # --------------------------------------------------------------- 通知链

    def submit_notification(self, *, request_id: str, actor_id: str, site_id: str,
                            patient_id: str, authorization_id: str, channel: str,
                            subject: str, content: str, not_before: str | None = None,
                            ttl_minutes: int = DEFAULT_TTL_MINUTES) -> dict[str, Any]:
        channel = self._id(channel, "channel")
        subject_text = self._text(subject, "subject", 200)
        content_text = self._text(content, "content", 4000)
        if ttl_minutes <= 0 or ttl_minutes > 7 * 24 * 60:
            raise ValidationError("ttl_minutes 不合法")
        start_moment = parse_utc(not_before) if not_before else self._now()
        expires = add_minutes(start_moment, ttl_minutes)
        # 内容摘要用于区分安全重放与内容冲突。
        content_hash = digest({"channel": channel, "subject": subject_text,
                               "content": content_text, "authorization_id": authorization_id})
        payload = {"actor_id": actor_id, "site_id": site_id, "patient_id": patient_id,
                   "authorization_id": authorization_id, "channel": channel,
                   "subject": subject_text, "content": content_text,
                   "not_before": format_utc(start_moment), "ttl_minutes": ttl_minutes,
                   "idempotency_key": request_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, NOTIFY_ROLES)
            site = self._site(conn, site_id)
            self._same_org(actor, site)
            patient = self._patient(conn, patient_id)
            if patient["site_id"] != site_id:
                raise ValidationError("患者与院区不匹配")
            auth = self._authorization(conn, authorization_id)
            if auth["patient_id"] != patient_id or auth["site_id"] != site_id:
                raise ValidationError("联络授权与患者或院区不匹配")
            if auth["revoked"]:
                raise PermissionDenied("联络授权已撤回，不能提交通知")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO ed_notification_requests(request_id,site_id,patient_id,"
                        "authorization_id,channel,subject,content,content_hash,idempotency_key,"
                        "not_before,expires_at,status,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
                        (request_id, site_id, patient_id, authorization_id, channel,
                         subject_text, content_text, content_hash, request_id,
                         format_utc(start_moment), format_utc(expires),
                         actor_id, self._now_str()),
                    )
                except Exception as exc:
                    # 同一 request_id 不同内容：明确的内容冲突。
                    raise DeliveryConflictError("通知请求编号已被不同内容使用") from exc
                conn.execute(
                    "INSERT INTO ed_notification_deliveries(delivery_id,request_id,attempt_no,"
                    "status,next_attempt_at,attempt_count) VALUES(?,?,1,'pending',?,0)",
                    (uuid.uuid4().hex, request_id, format_utc(start_moment)),
                )
                self._audit(conn, actor_id=actor_id, action="ed.notification.submitted",
                            resource_type="ed_notification", resource_id=request_id,
                            detail={"site_id": site_id, "patient_id": patient_id,
                                    "authorization_id": authorization_id, "channel": channel,
                                    "content_hash": content_hash,
                                    "expires_at": format_utc(expires)})
                return "ed_notification", request_id, {"request_id": request_id,
                                                       "status": "pending"}

            # request_id 即幂等键：相同内容安全重放，不同内容明确报内容冲突。
            return self._idempotent(conn, request_id=request_id, action="ed.submit_notification",
                                    payload=payload, create=create,
                                    conflict_error=DeliveryConflictError)

    def get_notification(self, request_id: str) -> NotificationRequest:
        r = self.database.connection.execute(
            "SELECT nr.*, nd.attempt_count, nd.detail AS delivery_detail "
            "FROM ed_notification_requests nr LEFT JOIN ed_notification_deliveries nd "
            "ON nd.request_id=nr.request_id WHERE nr.request_id=?",
            (request_id,),
        ).fetchone()
        if r is None:
            raise NotFoundError("通知请求不存在")
        return NotificationRequest(r["request_id"], r["site_id"], r["patient_id"],
                                   r["authorization_id"], r["channel"], r["subject"],
                                   r["content"], r["status"], r["not_before"], r["expires_at"],
                                   r["created_by"], r["created_at"], r["attempt_count"],
                                   r["delivery_detail"])

    def process_due_deliveries(self) -> dict[str, int]:
        """处理所有到期投递：窗口等待、有限重试、人工处置、撤回阻断。"""

        now = self._now()
        candidates = self.database.connection.execute(
            "SELECT d.request_id AS request_id FROM ed_notification_deliveries d "
            "JOIN ed_notification_requests r ON r.request_id=d.request_id "
            "WHERE d.status IN ('pending','retry') AND d.next_attempt_at<=?",
            (format_utc(now),),
        ).fetchall()
        result = {"delivered": 0, "retried": 0, "waiting_window": 0, "manual": 0,
                  "expired": 0, "blocked": 0}
        for candidate in candidates:
            outcome = self._process_one(candidate["request_id"])
            result[outcome] += 1
        return result

    def _process_one(self, request_id: str) -> str:
        with self.database.transaction(immediate=True) as conn:
            req = conn.execute("SELECT * FROM ed_notification_requests WHERE request_id=?",
                               (request_id,)).fetchone()
            delivery = conn.execute(
                "SELECT * FROM ed_notification_deliveries WHERE request_id=?", (request_id,)
            ).fetchone()
            if req is None or delivery["status"] not in ("pending", "retry"):
                return "delivered"
            now = self._now()
            stamp = format_utc(now)
            auth = self._authorization(conn, req["authorization_id"])
            site = self._site(conn, req["site_id"])

            # 授权撤回：立即阻断后续披露，不产生重试或人工任务。
            if auth["revoked"]:
                conn.execute("UPDATE ed_notification_deliveries SET status='blocked', detail=?,"
                             "attempted_at=? WHERE request_id=?",
                             ("authorization_revoked", stamp, request_id))
                conn.execute("UPDATE ed_notification_requests SET status='blocked' WHERE request_id=?",
                             (request_id,))
                self._audit(conn, actor_id=req["created_by"], action="ed.notification.blocked",
                            resource_type="ed_notification", resource_id=request_id,
                            detail={"reason": "authorization_revoked"})
                return "blocked"

            if now > parse_utc(req["expires_at"]):
                return self._to_manual(conn, req, delivery, "expired_unsent")

            local_now = now.astimezone(ZoneInfo(site["timezone_name"]))
            if not within_window(local_now, auth["window_start"], auth["window_end"]):
                next_open = next_window_open(now, site["timezone_name"],
                                             auth["window_start"], auth["window_end"])
                conn.execute("UPDATE ed_notification_deliveries SET status='pending',"
                             "next_attempt_at=? WHERE request_id=?",
                             (format_utc(next_open), request_id))
                return "waiting_window"

            attempts = delivery["attempt_count"]
            try:
                self.transport.send(channel=req["channel"], target=auth["contact_channel"],
                                    subject=req["subject"], content=req["content"])
            except Exception as exc:  # 投递失败：有限退避重试，之后转人工。
                next_count = attempts + 1
                if next_count >= self.max_attempts:
                    conn.execute(
                        "UPDATE ed_notification_deliveries SET status='failed', attempt_count=?,"
                        "detail=?, attempted_at=?, next_attempt_at=NULL WHERE request_id=?",
                        (next_count, str(exc)[:300], stamp, request_id),
                    )
                    return self._to_manual(conn, req, delivery, "delivery_failed",
                                           already_failed=True, detail=str(exc)[:300])
                backoff = BACKOFF_SECONDS[min(next_count - 1, len(BACKOFF_SECONDS) - 1)]
                next_at = add_minutes(now, backoff // 60 or 1)
                if next_at > parse_utc(req["expires_at"]):
                    conn.execute(
                        "UPDATE ed_notification_deliveries SET status='failed', attempt_count=?,"
                        "detail=?, attempted_at=?, next_attempt_at=NULL WHERE request_id=?",
                        (next_count, str(exc)[:300], stamp, request_id),
                    )
                    return self._to_manual(conn, req, delivery, "retry_window_exceeds_ttl",
                                           already_failed=True, detail=str(exc)[:300])
                conn.execute(
                    "UPDATE ed_notification_deliveries SET status='retry', attempt_count=?,"
                    "detail=?, attempted_at=?, next_attempt_at=? WHERE request_id=?",
                    (next_count, str(exc)[:300], stamp, format_utc(next_at), request_id),
                )
                self._audit(conn, actor_id=req["created_by"], action="ed.notification.retry_scheduled",
                            resource_type="ed_notification", resource_id=request_id,
                            detail={"attempt": next_count, "next_attempt_at": format_utc(next_at)})
                return "retried"

            # 投递成功：记录披露历史（授权撤回后此行仍保留）。
            conn.execute(
                "UPDATE ed_notification_deliveries SET status='delivered', attempt_count=?,"
                "detail=NULL, attempted_at=?, next_attempt_at=NULL WHERE request_id=?",
                (attempts + 1, stamp, request_id),
            )
            conn.execute("UPDATE ed_notification_requests SET status='delivered' WHERE request_id=?",
                         (request_id,))
            conn.execute(
                "INSERT INTO ed_authorization_access(access_id,authorization_id,"
                "notification_request_id,summary,disclosed_by,disclosed_at) "
                "VALUES(?,?,?,?,?,?)",
                (uuid.uuid4().hex, req["authorization_id"], request_id, req["subject"],
                 req["created_by"], stamp),
            )
            self._audit(conn, actor_id=req["created_by"], action="ed.notification.delivered",
                        resource_type="ed_notification", resource_id=request_id,
                        detail={"channel": req["channel"], "attempt": attempts + 1})
            return "delivered"

    def _to_manual(self, conn, req, delivery, reason: str, *, already_failed: bool = False,
                   detail: str | None = None) -> str:
        if not already_failed:
            conn.execute(
                "UPDATE ed_notification_deliveries SET status='failed', detail=?,"
                "attempted_at=?, next_attempt_at=NULL WHERE request_id=?",
                (detail or reason, format_utc(self._now()), req["request_id"]),
            )
        conn.execute("UPDATE ed_notification_requests SET status='failed' WHERE request_id=?",
                     (req["request_id"],))
        task_id = uuid.uuid4().hex
        deadline = add_minutes(self._now(), self.manual_sla_minutes)
        conn.execute(
            "INSERT INTO ed_manual_tasks(task_id,request_id,site_id,reason,status,deadline_at,"
            "created_at) VALUES(?,?,?,?,'open',?,?)",
            (task_id, req["request_id"], req["site_id"], reason, format_utc(deadline),
             self._now_str()),
        )
        self._audit(conn, actor_id=req["created_by"], action="ed.notification.manual_task_created",
                    resource_type="ed_manual_task", resource_id=task_id,
                    detail={"request_id": req["request_id"], "reason": reason,
                            "deadline_at": format_utc(deadline)})
        return "manual" if reason == "delivery_failed" else "expired"

    def resolve_manual_task(self, *, request_id: str, actor_id: str, task_id: str,
                            resolution_note: str) -> dict[str, Any]:
        note = self._text(resolution_note, "resolution_note", 1000)
        payload = {"actor_id": actor_id, "task_id": task_id, "resolution_note": note}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, WRITE_ROLES)
            row = conn.execute("SELECT * FROM ed_manual_tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise NotFoundError("人工处置任务不存在")
            self._same_org(actor, self._site(conn, row["site_id"]))
            if row["status"] != "open":
                raise ConflictError(f"任务当前状态为 {row['status']}")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "UPDATE ed_manual_tasks SET status='resolved', resolved_by=?, resolution_note=?,"
                    "resolved_at=? WHERE task_id=?",
                    (actor_id, note, self._now_str(), task_id),
                )
                self._audit(conn, actor_id=actor_id, action="ed.manual_task.resolved",
                            resource_type="ed_manual_task", resource_id=task_id,
                            detail={"request_id": row["request_id"]})
                return "ed_manual_task", task_id, {"task_id": task_id, "status": "resolved"}

            return self._idempotent(conn, request_id=request_id, action="ed.resolve_manual_task",
                                    payload=payload, create=create)

    def expire_manual_tasks(self) -> int:
        """超过处置期限未结办的任务关闭为 expired，不再无限等待。"""

        count = 0
        now = self._now()
        rows = self.database.connection.execute(
            "SELECT * FROM ed_manual_tasks WHERE status='open' AND deadline_at<?",
            (format_utc(now),),
        ).fetchall()
        for row in rows:
            with self.database.transaction(immediate=True) as conn:
                target = conn.execute("SELECT * FROM ed_manual_tasks WHERE task_id=?",
                                      (row["task_id"],)).fetchone()
                if target is None or target["status"] != "open":
                    continue
                conn.execute("UPDATE ed_manual_tasks SET status='expired' WHERE task_id=?",
                             (row["task_id"],))
                self._audit(conn, actor_id="system", action="ed.manual_task.expired",
                            resource_type="ed_manual_task", resource_id=row["task_id"],
                            detail={"request_id": row["request_id"]})
                count += 1
        return count

    def list_manual_tasks(self, site_id: str, status: str | None = None) -> list[ManualTask]:
        query = "SELECT * FROM ed_manual_tasks WHERE site_id=?"
        params: list[Any] = [site_id]
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY created_at, task_id"
        rows = self.database.connection.execute(query, params).fetchall()
        return [ManualTask(r["task_id"], r["request_id"], r["site_id"], r["reason"], r["status"],
                           r["deadline_at"], r["resolved_by"], r["resolution_note"],
                           r["created_at"], r["resolved_at"]) for r in rows]

    def run_maintenance(self) -> dict[str, int]:
        """进程重启后调用即可继续处理未决投递与到期人工任务。"""

        result = self.process_due_deliveries()
        result["manual_expired"] = self.expire_manual_tasks()
        return result

    # ------------------------------------------------------------- 后台视图

    def current_responsibility(self, site_id: str) -> dict[str, Any]:
        now = self._now()
        with self.database.transaction() as conn:
            site = self._site(conn, site_id)
            row = self._current_shift_row(conn, site_id, now)
            if row is None:
                return {"site_id": site_id, "as_of": format_utc(now), "current_shift": None,
                        "responsible_actors": []}
            assignments = []
            for a in conn.execute(
                "SELECT * FROM ed_shift_assignments WHERE shift_id=? ORDER BY sequence_no",
                (row["shift_id"],),
            ).fetchall():
                assignments.append({
                    "position_code": a["position_code"],
                    "actor_id": a["holder_actor_id"],
                    "qualification_valid": self._qualified(
                        conn, site_id=site_id, actor_id=a["holder_actor_id"],
                        position_code=a["position_code"], moment=now),
                })
            return {"site_id": site_id, "as_of": format_utc(now),
                    "current_shift": {"shift_id": row["shift_id"], "shift_date": row["shift_date"],
                                      "version": row["version"], "starts_at": row["starts_at"],
                                      "ends_at": row["ends_at"],
                                      "timezone_name": site["timezone_name"]},
                    "responsible_actors": assignments}

    def disclosure_scope(self, patient_id: str) -> list[dict[str, Any]]:
        """当前可披露范围：仅未撤回授权，含窗口与范围。"""

        rows = self.database.connection.execute(
            "SELECT * FROM ed_contact_authorizations WHERE patient_id=? AND revoked=0 "
            "ORDER BY granted_at",
            (patient_id,),
        ).fetchall()
        return [{"authorization_id": r["authorization_id"], "contact_name": r["contact_name"],
                 "contact_channel": r["contact_channel"], "scope": json.loads(r["scope_json"]),
                 "window_start": r["window_start"], "window_end": r["window_end"]} for r in rows]

    def list_pending_contacts(self, site_id: str) -> dict[str, list[dict[str, Any]]]:
        requests_rows = self.database.connection.execute(
            "SELECT * FROM ed_notification_requests WHERE site_id=? AND status IN ('pending','failed') "
            "ORDER BY created_at",
            (site_id,),
        ).fetchall()
        tasks_rows = self.database.connection.execute(
            "SELECT * FROM ed_manual_tasks WHERE site_id=? AND status='open' ORDER BY deadline_at",
            (site_id,),
        ).fetchall()
        return {
            "pending_requests": [{"request_id": r["request_id"], "patient_id": r["patient_id"],
                                  "status": r["status"], "channel": r["channel"],
                                  "not_before": r["not_before"], "expires_at": r["expires_at"]}
                                 for r in requests_rows],
            "open_manual_tasks": [{"task_id": r["task_id"], "request_id": r["request_id"],
                                   "reason": r["reason"], "deadline_at": r["deadline_at"]}
                                  for r in tasks_rows],
        }

    def duty_snapshot(self, site_id: str) -> DutySnapshot:
        responsibility = self.current_responsibility(site_id)
        patients = self.database.connection.execute(
            "SELECT patient_id FROM ed_patients WHERE site_id=?", (site_id,)
        ).fetchall()
        scopes: list[dict[str, Any]] = []
        for p in patients:
            for item in self.disclosure_scope(p["patient_id"]):
                scopes.append({"patient_id": p["patient_id"], **item})
        pending = self.list_pending_contacts(site_id)
        pending_flat = [*pending["pending_requests"], *pending["open_manual_tasks"]]
        return DutySnapshot(site_id, responsibility["as_of"], responsibility["current_shift"],
                            tuple(responsibility["responsible_actors"]), tuple(scopes),
                            tuple(pending_flat))
