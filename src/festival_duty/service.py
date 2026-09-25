"""急诊节日值守与家属联络领域服务。

在基础层（组织、站点、操作者、权限、幂等回执、审计链）之上，把班次版本、
岗位资格、患者联络授权、通知窗口和交班事项纳入同一业务链：

- 临时换岗只有在接任者资格有效且双方确认后才生效；
- 抢救中的事项在交班时自动保留，只能凭理由显式转移；
- 授权撤回立即阻止后续披露，但历史披露记录保留可查；
- 通知请求区分安全重放与内容冲突，失败投递限次后进入有期限的人工处置；
- 跨午夜班次按站点时区归属服务日；
- 全部状态落在 SQLite，进程重启后通过 recover() 继续处理未完成事项。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from festival_foundation.audit import append_event, digest
from festival_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from festival_foundation.models import Actor, WriteReceipt
from festival_foundation.service import DomainService

from .gateway import DeliveryFailure, DeliveryGateway, LoopbackGateway
from .models import (DECISIONS, ITEM_STATUSES, POSITIONS, SCOPES, Authorization,
                     DisclosureEvent, HandoverDecision, HandoverItem, Notification, Shift)
from .schema import ensure_schema


class DutyService:
    """协调急诊值守与家属联络的权限、幂等、事务和审计规则。"""

    def __init__(self, foundation: DomainService, gateway: DeliveryGateway | None = None,
                 max_attempts: int = 3, manual_ttl_seconds: int = 4 * 3600) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于 0")
        if manual_ttl_seconds < 60:
            raise ValueError("manual_ttl_seconds 至少为 60 秒")
        self.foundation = foundation
        self.database = foundation.database
        self.clock = foundation.clock
        self.gateway = gateway or LoopbackGateway()
        self.max_attempts = max_attempts
        self.manual_ttl = timedelta(seconds=manual_ttl_seconds)
        ensure_schema(self.database)

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc).replace(microsecond=0)

    def _iso(self, moment: datetime) -> str:
        return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _parse_time(self, value: Any, field: str) -> datetime:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
        if moment.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return moment.astimezone(timezone.utc).replace(microsecond=0)

    def _actor(self, connection, actor_id: str) -> Actor:
        return self.foundation._actor(connection, actor_id)

    def _site(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("站点不存在")
        return row

    def _check_org(self, actor: Actor, site_row) -> None:
        if actor.role != "admin" and actor.organization_id != site_row["organization_id"]:
            raise PermissionDenied("不能操作其他组织的站点")

    def _service_date(self, site_row, start: datetime) -> str:
        """跨午夜班次按站点时区归属到班次开始所在的服务日。"""

        try:
            zone = ZoneInfo(site_row["timezone_name"])
        except ZoneInfoNotFoundError as exc:
            raise ValidationError("站点时区无法识别") from exc
        return start.astimezone(zone).date().isoformat()

    @staticmethod
    def _rowdict(row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    # ------------------------------------------------------------------
    # 岗位资格
    # ------------------------------------------------------------------

    def _qualified(self, connection, actor_id: str, position: str, moment: datetime) -> bool:
        row = connection.execute(
            "SELECT 1 FROM duty_qualifications WHERE actor_id=? AND position=? "
            "AND revoked_at IS NULL AND expires_at>? LIMIT 1",
            (actor_id, position, self._iso(moment)),
        ).fetchone()
        return row is not None

    def _require_qualified(self, connection, actor_id: str, position: str, moment: datetime) -> None:
        if not self._qualified(connection, actor_id, position, moment):
            raise PermissionDenied("岗位资格无效或已过期")

    def grant_qualification(self, *, request_id: str, actor_id: str, target_actor_id: str,
                            position: str, expires_at: str,
                            qualification_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "target_actor_id": target_actor_id, "position": position,
                   "expires_at": expires_at, "qualification_id": qualification_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.foundation._require(actor, "admin")
            self._actor(connection, target_actor_id)
            if position not in POSITIONS:
                raise ValidationError("岗位不在允许范围内")
            expires = self._parse_time(expires_at, "expires_at")
            if expires <= self._now():
                raise ValidationError("资格有效期必须晚于当前时间")
            qualification_id = (self.foundation._identifier(qualification_id, "qualification_id")
                                if qualification_id else uuid.uuid4().hex)
            now = self._iso(self._now())

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO duty_qualifications(qualification_id,actor_id,position,granted_by,granted_at,"
                        "expires_at) VALUES(?,?,?,?,?,?)",
                        (qualification_id, target_actor_id, position, actor_id, now, self._iso(expires)),
                    )
                except Exception as exc:
                    raise ConflictError("资格编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="duty.qualification_granted",
                             resource_type="duty_qualification", resource_id=qualification_id,
                             detail={"target_actor_id": target_actor_id, "position": position,
                                     "expires_at": self._iso(expires)},
                             occurred_at=now)
                return "duty_qualification", qualification_id, {"qualification_id": qualification_id}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="duty.grant_qualification", payload=payload, create=create)

    def revoke_qualification(self, *, request_id: str, actor_id: str, qualification_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "qualification_id": qualification_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.foundation._require(actor, "admin")
            row = connection.execute(
                "SELECT * FROM duty_qualifications WHERE qualification_id=?", (qualification_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("岗位资格不存在")
            if row["revoked_at"] is not None:
                raise ConflictError("岗位资格已撤回")
            now = self._iso(self._now())

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE duty_qualifications SET revoked_at=?, revoked_by=? WHERE qualification_id=?",
                    (now, actor_id, qualification_id),
                )
                append_event(connection, actor_id=actor_id, action="duty.qualification_revoked",
                             resource_type="duty_qualification", resource_id=qualification_id,
                             detail={"target_actor_id": row["actor_id"], "position": row["position"]},
                             occurred_at=now)
                return "duty_qualification", qualification_id, {"qualification_id": qualification_id}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="duty.revoke_qualification", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 班次与临时换岗
    # ------------------------------------------------------------------

    def _shift(self, connection, shift_id: str):
        row = connection.execute("SELECT * FROM duty_shifts WHERE shift_id=?", (shift_id,)).fetchone()
        if row is None:
            raise NotFoundError("班次不存在")
        return row

    def create_shift(self, *, request_id: str, actor_id: str, site_id: str, position: str,
                     holder_actor_id: str, starts_at: str, ends_at: str,
                     shift_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "position": position,
                   "holder_actor_id": holder_actor_id, "starts_at": starts_at,
                   "ends_at": ends_at, "shift_id": shift_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.foundation._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._check_org(actor, site)
            if position not in POSITIONS:
                raise ValidationError("岗位不在允许范围内")
            self._actor(connection, holder_actor_id)
            start = self._parse_time(starts_at, "starts_at")
            end = self._parse_time(ends_at, "ends_at")
            if end <= start:
                raise ValidationError("班次结束时间必须晚于开始时间")
            self._require_qualified(connection, holder_actor_id, position, start)
            service_date = self._service_date(site, start)
            shift_id = self.foundation._identifier(shift_id, "shift_id") if shift_id else uuid.uuid4().hex
            now = self._iso(self._now())

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO duty_shifts(shift_id,site_id,position,holder_actor_id,starts_at,ends_at,"
                        "service_date,timezone_name,version,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,1,?,?)",
                        (shift_id, site_id, position, holder_actor_id, self._iso(start), self._iso(end),
                         service_date, site["timezone_name"], actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("班次编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="duty.shift_scheduled",
                             resource_type="duty_shift", resource_id=shift_id,
                             detail={"site_id": site_id, "position": position,
                                     "holder_actor_id": holder_actor_id, "starts_at": self._iso(start),
                                     "ends_at": self._iso(end), "service_date": service_date, "version": 1},
                             occurred_at=now)
                return "duty_shift", shift_id, {"shift_id": shift_id, "service_date": service_date, "version": 1}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="duty.create_shift", payload=payload, create=create)

    def request_swap(self, *, request_id: str, actor_id: str, shift_id: str, to_actor_id: str,
                     reason: str = "", swap_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "shift_id": shift_id, "to_actor_id": to_actor_id,
                   "reason": reason, "swap_id": swap_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.foundation._require(actor, "admin", "operator")
            shift = self._shift(connection, shift_id)
            site = self._site(connection, shift["site_id"])
            self._check_org(actor, site)
            now_moment = self._now()
            now = self._iso(now_moment)
            if shift["ends_at"] <= now:
                raise ValidationError("班次已结束，无法换岗")
            from_actor_id = shift["holder_actor_id"]
            if to_actor_id == from_actor_id:
                raise ValidationError("接任人不能是当前持有人")
            self._actor(connection, to_actor_id)
            # 发起时先做一次资格校验，尽早暴露问题；生效时会再次校验。
            self._require_qualified(connection, to_actor_id, shift["position"], now_moment)
            pending = connection.execute(
                "SELECT 1 FROM duty_swap_requests WHERE shift_id=? AND status='pending'", (shift_id,)
            ).fetchone()
            if pending:
                raise ConflictError("该班次已存在待确认的换岗请求")
            reason = str(reason).strip()
            if len(reason) > 200:
                raise ValidationError("reason 不能超过 200 个字符")
            swap_id = self.foundation._identifier(swap_id, "swap_id") if swap_id else uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO duty_swap_requests(swap_id,shift_id,from_actor_id,to_actor_id,status,reason,"
                        "created_by,created_at) VALUES(?,?,?,?,'pending',?,?,?)",
                        (swap_id, shift_id, from_actor_id, to_actor_id, reason, actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("换岗请求编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="duty.swap_requested",
                             resource_type="duty_swap", resource_id=swap_id,
                             detail={"shift_id": shift_id, "from_actor_id": from_actor_id,
                                     "to_actor_id": to_actor_id, "reason": reason},
                             occurred_at=now)
                return "duty_swap", swap_id, {"swap_id": swap_id}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="duty.request_swap", payload=payload, create=create)

    def _swap(self, connection, swap_id: str):
        row = connection.execute(
            "SELECT * FROM duty_swap_requests WHERE swap_id=?", (swap_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("换岗请求不存在")
        return row

    def _swap_state(self, connection, swap) -> dict[str, Any]:
        state = {"swap_id": swap["swap_id"], "shift_id": swap["shift_id"], "status": swap["status"],
                 "from_actor_id": swap["from_actor_id"], "to_actor_id": swap["to_actor_id"],
                 "from_confirmed": swap["from_confirmed_at"] is not None,
                 "to_confirmed": swap["to_confirmed_at"] is not None}
        if swap["status"] == "applied":
            state["shift_version"] = self._shift(connection, swap["shift_id"])["version"]
        return state

    def confirm_swap(self, *, actor_id: str, swap_id: str) -> dict[str, Any]:
        """记录一方确认；双方确认且接任者资格在生效时仍有效才换岗。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            swap = self._swap(connection, swap_id)
            parties = (swap["from_actor_id"], swap["to_actor_id"])
            if swap["status"] == "applied" and actor.actor_id in parties:
                return self._swap_state(connection, swap)
            if swap["status"] != "pending":
                raise ConflictError("换岗请求已结束")
            if actor.actor_id not in parties:
                raise PermissionDenied("只有换岗双方可以确认")
            now_moment = self._now()
            now = self._iso(now_moment)
            if actor.actor_id == swap["from_actor_id"] and swap["from_confirmed_at"] is None:
                connection.execute(
                    "UPDATE duty_swap_requests SET from_confirmed_at=? WHERE swap_id=?", (now, swap_id))
                append_event(connection, actor_id=actor_id, action="duty.swap_confirmed",
                             resource_type="duty_swap", resource_id=swap_id,
                             detail={"party": actor.actor_id, "shift_id": swap["shift_id"]}, occurred_at=now)
            if actor.actor_id == swap["to_actor_id"] and swap["to_confirmed_at"] is None:
                connection.execute(
                    "UPDATE duty_swap_requests SET to_confirmed_at=? WHERE swap_id=?", (now, swap_id))
                append_event(connection, actor_id=actor_id, action="duty.swap_confirmed",
                             resource_type="duty_swap", resource_id=swap_id,
                             detail={"party": actor.actor_id, "shift_id": swap["shift_id"]}, occurred_at=now)
            swap = self._swap(connection, swap_id)
            if swap["from_confirmed_at"] is None or swap["to_confirmed_at"] is None:
                return self._swap_state(connection, swap)
            shift = self._shift(connection, swap["shift_id"])
            if shift["ends_at"] <= now:
                raise ValidationError("班次已结束，换岗无法生效")
            # 生效前再次校验接任者资格；资格失效则整个确认回滚，换岗不生效。
            self._require_qualified(connection, swap["to_actor_id"], shift["position"], now_moment)
            version = shift["version"] + 1
            connection.execute(
                "UPDATE duty_shifts SET holder_actor_id=?, version=? WHERE shift_id=?",
                (swap["to_actor_id"], version, swap["shift_id"]),
            )
            connection.execute(
                "UPDATE duty_swap_requests SET status='applied', decided_at=? WHERE swap_id=?", (now, swap_id))
            append_event(connection, actor_id=actor_id, action="duty.swap_applied",
                         resource_type="duty_shift", resource_id=swap["shift_id"],
                         detail={"swap_id": swap_id, "from_actor_id": swap["from_actor_id"],
                                 "to_actor_id": swap["to_actor_id"], "version": version},
                         occurred_at=now)
            return self._swap_state(connection, self._swap(connection, swap_id))

    def decline_swap(self, *, actor_id: str, swap_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            swap = self._swap(connection, swap_id)
            if swap["status"] != "pending":
                raise ConflictError("换岗请求已结束")
            if actor.actor_id not in (swap["from_actor_id"], swap["to_actor_id"]):
                raise PermissionDenied("只有换岗双方可以拒绝")
            now = self._iso(self._now())
            connection.execute(
                "UPDATE duty_swap_requests SET status='declined', decided_at=? WHERE swap_id=?", (now, swap_id))
            append_event(connection, actor_id=actor_id, action="duty.swap_declined",
                         resource_type="duty_swap", resource_id=swap_id,
                         detail={"party": actor.actor_id, "shift_id": swap["shift_id"]}, occurred_at=now)
            return {"swap_id": swap_id, "shift_id": swap["shift_id"], "status": "declined"}

    # ------------------------------------------------------------------
    # 患者联络授权与披露
    # ------------------------------------------------------------------

    def _authorization(self, connection, authorization_id: str):
        row = connection.execute(
            "SELECT * FROM contact_authorizations WHERE authorization_id=?", (authorization_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("联络授权不存在")
        return row

    def grant_authorization(self, *, request_id: str, actor_id: str, site_id: str, patient_ref: str,
                            contact_name: str, contact_channel: str, scope: str,
                            authorization_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "patient_ref": patient_ref,
                   "contact_name": contact_name, "contact_channel": contact_channel,
                   "scope": scope, "authorization_id": authorization_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.foundation._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._check_org(actor, site)
            patient_ref = self.foundation._text(patient_ref, "patient_ref", 120)
            contact_name = self.foundation._text(contact_name, "contact_name", 120)
            contact_channel = self.foundation._text(contact_channel, "contact_channel", 60)
            if scope not in SCOPES:
                raise ValidationError("披露范围不在允许范围内")
            authorization_id = (self.foundation._identifier(authorization_id, "authorization_id")
                                if authorization_id else uuid.uuid4().hex)
            now = self._iso(self._now())

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO contact_authorizations(authorization_id,site_id,patient_ref,contact_name,"
                        "contact_channel,scope,status,granted_by,granted_at) VALUES(?,?,?,?,?,?,'active',?,?)",
                        (authorization_id, site_id, patient_ref, contact_name, contact_channel,
                         scope, actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("授权编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="duty.authorization_granted",
                             resource_type="contact_authorization", resource_id=authorization_id,
                             detail={"site_id": site_id, "patient_ref": patient_ref,
                                     "contact_name": contact_name, "scope": scope},
                             occurred_at=now)
                return "contact_authorization", authorization_id, {"authorization_id": authorization_id}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="duty.grant_authorization", payload=payload, create=create)

    def revoke_authorization(self, *, request_id: str, actor_id: str, authorization_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "authorization_id": authorization_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.foundation._require(actor, "admin", "operator")
            authorization = self._authorization(connection, authorization_id)
            site = self._site(connection, authorization["site_id"])
            self._check_org(actor, site)
            if authorization["status"] == "revoked":
                raise ConflictError("授权已撤回")
            now = self._iso(self._now())

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE contact_authorizations SET status='revoked', revoked_by=?, revoked_at=? "
                    "WHERE authorization_id=?",
                    (actor_id, now, authorization_id),
                )
                append_event(connection, actor_id=actor_id, action="duty.authorization_revoked",
                             resource_type="contact_authorization", resource_id=authorization_id,
                             detail={"patient_ref": authorization["patient_ref"],
                                     "contact_name": authorization["contact_name"]},
                             occurred_at=now)
                return "contact_authorization", authorization_id, {"authorization_id": authorization_id}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="duty.revoke_authorization", payload=payload, create=create)

    def _record_disclosure(self, connection, *, authorization, notification_id: str | None,
                           channel: str, actor_id: str, occurred_at: str) -> None:
        disclosure_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO disclosure_events(disclosure_id,authorization_id,notification_id,site_id,patient_ref,"
            "contact_name,scope,channel,disclosed_by,disclosed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (disclosure_id, authorization["authorization_id"], notification_id, authorization["site_id"],
             authorization["patient_ref"], authorization["contact_name"], authorization["scope"],
             channel, actor_id, occurred_at),
        )

    # ------------------------------------------------------------------
    # 通知请求与投递
    # ------------------------------------------------------------------

    def _notification(self, connection, notification_id: str):
        row = connection.execute(
            "SELECT * FROM notification_requests WHERE notification_id=?", (notification_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("通知请求不存在")
        return row

    def request_notification(self, *, request_id: str, actor_id: str, site_id: str, patient_ref: str,
                             authorization_id: str, scope: str, summary: str,
                             window_start: str, window_end: str,
                             notification_id: str | None = None,
                             max_attempts: int | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "patient_ref": patient_ref,
                   "authorization_id": authorization_id, "scope": scope, "summary": summary,
                   "window_start": window_start, "window_end": window_end,
                   "notification_id": notification_id, "max_attempts": max_attempts}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.foundation._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._check_org(actor, site)
            authorization = self._authorization(connection, authorization_id)
            if authorization["site_id"] != site_id or authorization["patient_ref"] != patient_ref:
                raise ValidationError("授权与患者或站点不匹配")
            if authorization["status"] != "active":
                raise PermissionDenied("授权已撤回，禁止新的披露")
            if scope not in SCOPES:
                raise ValidationError("披露范围不在允许范围内")
            if SCOPES[scope] > SCOPES[authorization["scope"]]:
                raise PermissionDenied("请求范围超出授权可披露范围")
            summary = self.foundation._text(summary, "summary", 500)
            start = self._parse_time(window_start, "window_start")
            end = self._parse_time(window_end, "window_end")
            if end <= start:
                raise ValidationError("通知窗口结束时间必须晚于开始时间")
            attempts_limit = self.max_attempts if max_attempts is None else int(max_attempts)
            if not 1 <= attempts_limit <= 20:
                raise ValidationError("max_attempts 必须在 1 到 20 之间")
            notification_id = (self.foundation._identifier(notification_id, "notification_id")
                               if notification_id else uuid.uuid4().hex)
            content_hash = digest({"patient_ref": patient_ref, "authorization_id": authorization_id,
                                   "scope": scope, "summary": summary,
                                   "window_start": self._iso(start), "window_end": self._iso(end)})
            now = self._iso(self._now())

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO notification_requests(notification_id,site_id,patient_ref,authorization_id,"
                        "scope,summary,content_hash,window_start,window_end,status,attempts,max_attempts,"
                        "created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'pending',0,?,?,?,?)",
                        (notification_id, site_id, patient_ref, authorization_id, scope, summary, content_hash,
                         self._iso(start), self._iso(end), attempts_limit, actor_id, now, now),
                    )
                except Exception as exc:
                    raise ConflictError("通知编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="duty.notification_requested",
                             resource_type="duty_notification", resource_id=notification_id,
                             detail={"site_id": site_id, "patient_ref": patient_ref,
                                     "authorization_id": authorization_id, "scope": scope,
                                     "content_hash": content_hash, "window_start": self._iso(start),
                                     "window_end": self._iso(end), "max_attempts": attempts_limit},
                             occurred_at=now)
                return "duty_notification", notification_id, {"notification_id": notification_id}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="duty.request_notification", payload=payload, create=create)

    def _record_attempt(self, connection, *, notification_id: str, actor_id: str,
                        outcome: str, detail: str | None, occurred_at: str) -> None:
        connection.execute(
            "INSERT INTO notification_attempts(attempt_id,notification_id,attempted_by,attempted_at,outcome,"
            "detail) VALUES(?,?,?,?,?,?)",
            (uuid.uuid4().hex, notification_id, actor_id, occurred_at, outcome, detail),
        )

    def _to_manual_handling(self, connection, *, notification, actor_id: str,
                            reason: str, now_moment: datetime) -> str:
        now = self._iso(now_moment)
        due = self._iso(now_moment + self.manual_ttl)
        connection.execute(
            "UPDATE notification_requests SET status='manual_handling', manual_due_at=?, updated_at=? "
            "WHERE notification_id=?",
            (due, now, notification["notification_id"]),
        )
        append_event(connection, actor_id=actor_id, action="duty.notification_manual_handling",
                     resource_type="duty_notification", resource_id=notification["notification_id"],
                     detail={"reason": reason, "attempts": notification["attempts"],
                             "manual_due_at": due},
                     occurred_at=now)
        return due

    def deliver_notification(self, *, actor_id: str, notification_id: str) -> dict[str, Any]:
        """在通知窗口内执行一次投递；失败限次后进入有期限的人工处置。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.foundation._require(actor, "admin", "operator")
            notification = self._notification(connection, notification_id)
            site = self._site(connection, notification["site_id"])
            self._check_org(actor, site)
            if notification["status"] == "delivered":
                raise ConflictError("通知已投递")
            if notification["status"] in ("expired", "abandoned"):
                raise ConflictError("通知已关闭")
            now_moment = self._now()
            now = self._iso(now_moment)
            if notification["status"] == "manual_handling":
                raise ConflictError("通知已进入人工处置，请通过人工办结处理")
            if now < notification["window_start"]:
                raise ValidationError("当前时间早于通知窗口")
            if now > notification["window_end"]:
                due = self._to_manual_handling(connection, notification=notification, actor_id=actor_id,
                                               reason="window_missed", now_moment=now_moment)
                return {"notification_id": notification_id, "status": "manual_handling",
                        "reason": "window_missed", "manual_due_at": due,
                        "attempts": notification["attempts"]}
            authorization = self._authorization(connection, notification["authorization_id"])
            if authorization["status"] != "active":
                # 授权撤回立即阻止披露，不消耗投递次数。
                raise PermissionDenied("授权已撤回，禁止披露")
            attempts = notification["attempts"] + 1
            try:
                channel = self.gateway.deliver(notification=self._rowdict(notification),
                                               authorization=self._rowdict(authorization),
                                               summary=notification["summary"])
            except DeliveryFailure as exc:
                self._record_attempt(connection, notification_id=notification_id, actor_id=actor_id,
                                     outcome="failed", detail=str(exc), occurred_at=now)
                connection.execute(
                    "UPDATE notification_requests SET attempts=?, updated_at=? WHERE notification_id=?",
                    (attempts, now, notification_id),
                )
                if attempts >= notification["max_attempts"]:
                    refreshed = self._notification(connection, notification_id)
                    due = self._to_manual_handling(connection, notification=refreshed, actor_id=actor_id,
                                                   reason="delivery_failed", now_moment=now_moment)
                    return {"notification_id": notification_id, "status": "manual_handling",
                            "reason": "delivery_failed", "manual_due_at": due, "attempts": attempts}
                append_event(connection, actor_id=actor_id, action="duty.notification_delivery_failed",
                             resource_type="duty_notification", resource_id=notification_id,
                             detail={"attempts": attempts, "error": str(exc)}, occurred_at=now)
                return {"notification_id": notification_id, "status": "pending", "attempts": attempts}
            self._record_attempt(connection, notification_id=notification_id, actor_id=actor_id,
                                 outcome="delivered", detail=channel, occurred_at=now)
            connection.execute(
                "UPDATE notification_requests SET status='delivered', attempts=?, updated_at=? "
                "WHERE notification_id=?",
                (attempts, now, notification_id),
            )
            self._record_disclosure(connection, authorization=authorization,
                                    notification_id=notification_id, channel=channel,
                                    actor_id=actor_id, occurred_at=now)
            append_event(connection, actor_id=actor_id, action="duty.notification_delivered",
                         resource_type="duty_notification", resource_id=notification_id,
                         detail={"authorization_id": authorization["authorization_id"],
                                 "scope": notification["scope"], "channel": channel,
                                 "attempts": attempts},
                         occurred_at=now)
            return {"notification_id": notification_id, "status": "delivered", "attempts": attempts}

    def resolve_manual_notification(self, *, actor_id: str, notification_id: str,
                                    outcome: str, note: str = "") -> dict[str, Any]:
        """人工办结处于处置队列中的通知，处置本身有期限。"""

        if outcome not in ("delivered", "abandoned"):
            raise ValidationError("outcome 必须是 delivered 或 abandoned")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.foundation._require(actor, "admin", "operator", "reviewer")
            notification = self._notification(connection, notification_id)
            site = self._site(connection, notification["site_id"])
            self._check_org(actor, site)
            if notification["status"] != "manual_handling":
                raise ConflictError("通知不在人工处置中")
            now_moment = self._now()
            now = self._iso(now_moment)
            if notification["manual_due_at"] is not None and now > notification["manual_due_at"]:
                connection.execute(
                    "UPDATE notification_requests SET status='expired', updated_at=? WHERE notification_id=?",
                    (now, notification_id),
                )
                append_event(connection, actor_id=actor_id, action="duty.notification_expired",
                             resource_type="duty_notification", resource_id=notification_id,
                             detail={"reason": "manual_deadline_missed"}, occurred_at=now)
                return {"notification_id": notification_id, "status": "expired"}
            note = str(note).strip()
            if len(note) > 200:
                raise ValidationError("note 不能超过 200 个字符")
            if outcome == "delivered":
                authorization = self._authorization(connection, notification["authorization_id"])
                if authorization["status"] != "active":
                    raise PermissionDenied("授权已撤回，禁止披露")
                connection.execute(
                    "UPDATE notification_requests SET status='delivered', updated_at=? WHERE notification_id=?",
                    (now, notification_id),
                )
                self._record_disclosure(connection, authorization=authorization,
                                        notification_id=notification_id, channel="manual",
                                        actor_id=actor_id, occurred_at=now)
            else:
                connection.execute(
                    "UPDATE notification_requests SET status='abandoned', updated_at=? WHERE notification_id=?",
                    (now, notification_id),
                )
            append_event(connection, actor_id=actor_id, action="duty.notification_manual_resolved",
                         resource_type="duty_notification", resource_id=notification_id,
                         detail={"outcome": outcome, "note": note}, occurred_at=now)
            return {"notification_id": notification_id, "status": outcome}

    def process_pending(self, *, actor_id: str = "system") -> dict[str, int]:
        """推进未决联络：窗口已过转人工处置，处置超期则终止为过期。"""

        now_moment = self._now()
        now = self._iso(now_moment)
        moved = 0
        expired = 0
        with self.database.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT * FROM notification_requests WHERE status='pending' AND window_end<?", (now,)
            ).fetchall()
            for notification in rows:
                self._to_manual_handling(connection, notification=notification, actor_id=actor_id,
                                         reason="window_missed", now_moment=now_moment)
                moved += 1
            rows = connection.execute(
                "SELECT * FROM notification_requests WHERE status='manual_handling' AND manual_due_at<?",
                (now,),
            ).fetchall()
            for notification in rows:
                connection.execute(
                    "UPDATE notification_requests SET status='expired', updated_at=? WHERE notification_id=?",
                    (now, notification["notification_id"]),
                )
                append_event(connection, actor_id=actor_id, action="duty.notification_expired",
                             resource_type="duty_notification",
                             resource_id=notification["notification_id"],
                             detail={"reason": "manual_deadline_missed"}, occurred_at=now)
                expired += 1
        return {"moved_to_manual": moved, "expired": expired}

    def recover(self) -> dict[str, int]:
        """进程重启后继续处理尚未完成的联络事项。"""

        return self.process_pending(actor_id="system")

    def sweep(self, *, actor_id: str) -> dict[str, int]:
        """供后台接口触发的一次未决联络推进。"""

        actor = self._actor(self.database.connection, actor_id)
        self.foundation._require(actor, "admin", "operator")
        return self.process_pending(actor_id=actor_id)

    # ------------------------------------------------------------------
    # 交班事项与交班决定
    # ------------------------------------------------------------------

    def _item(self, connection, item_id: str):
        row = connection.execute("SELECT * FROM handover_items WHERE item_id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("交班事项不存在")
        return row

    def create_handover_item(self, *, request_id: str, actor_id: str, site_id: str, shift_id: str,
                             patient_ref: str, category: str, summary: str,
                             in_rescue: bool = False, item_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "shift_id": shift_id,
                   "patient_ref": patient_ref, "category": category, "summary": summary,
                   "in_rescue": bool(in_rescue), "item_id": item_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.foundation._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._check_org(actor, site)
            shift = self._shift(connection, shift_id)
            if shift["site_id"] != site_id:
                raise ValidationError("班次不属于该站点")
            if category not in POSITIONS:
                raise ValidationError("事项类别不在允许范围内")
            patient_ref = self.foundation._text(patient_ref, "patient_ref", 120)
            summary = self.foundation._text(summary, "summary", 500)
            item_id = self.foundation._identifier(item_id, "item_id") if item_id else uuid.uuid4().hex
            status = "in_rescue" if in_rescue else "open"
            now = self._iso(self._now())

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO handover_items(item_id,site_id,shift_id,patient_ref,category,summary,status,"
                        "created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (item_id, site_id, shift_id, patient_ref, category, summary, status,
                         actor_id, now, now),
                    )
                except Exception as exc:
                    raise ConflictError("事项编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="duty.handover_item_created",
                             resource_type="handover_item", resource_id=item_id,
                             detail={"site_id": site_id, "shift_id": shift_id, "patient_ref": patient_ref,
                                     "category": category, "status": status},
                             occurred_at=now)
                return "handover_item", item_id, {"item_id": item_id, "status": status}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="duty.create_handover_item", payload=payload, create=create)

    def set_item_status(self, *, actor_id: str, item_id: str, status: str) -> dict[str, Any]:
        if status not in ITEM_STATUSES:
            raise ValidationError("事项状态不在允许范围内")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.foundation._require(actor, "admin", "operator")
            item = self._item(connection, item_id)
            site = self._site(connection, item["site_id"])
            self._check_org(actor, site)
            if item["status"] == status:
                return {"item_id": item_id, "status": status}
            if item["status"] == "closed":
                raise ConflictError("事项已关闭")
            now = self._iso(self._now())
            connection.execute(
                "UPDATE handover_items SET status=?, updated_at=? WHERE item_id=?", (status, now, item_id))
            append_event(connection, actor_id=actor_id, action="duty.handover_item_updated",
                         resource_type="handover_item", resource_id=item_id,
                         detail={"from_status": item["status"], "to_status": status}, occurred_at=now)
            return {"item_id": item_id, "status": status}

    def _record_decision(self, connection, *, batch_id: str, site_id: str, item_id: str,
                         from_shift_id: str, to_shift_id: str | None, decision: str,
                         reason: str | None, actor_id: str, occurred_at: str) -> str:
        decision_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO handover_decisions(decision_id,batch_id,site_id,item_id,from_shift_id,to_shift_id,"
            "decision,reason,decided_by,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (decision_id, batch_id, site_id, item_id, from_shift_id, to_shift_id,
             decision, reason, actor_id, occurred_at),
        )
        return decision_id

    def perform_handover(self, *, request_id: str, actor_id: str, from_shift_id: str,
                         to_shift_id: str) -> dict[str, Any]:
        """批量交班：普通事项随班次转移，抢救中的事项自动保留并记录决定。"""

        payload = {"actor_id": actor_id, "from_shift_id": from_shift_id, "to_shift_id": to_shift_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.foundation._require(actor, "admin", "operator")
            from_shift = self._shift(connection, from_shift_id)
            to_shift = self._shift(connection, to_shift_id)
            if from_shift_id == to_shift_id:
                raise ValidationError("交班班次不能相同")
            if from_shift["site_id"] != to_shift["site_id"]:
                raise ValidationError("交班班次必须属于同一站点")
            if from_shift["position"] != to_shift["position"]:
                raise ValidationError("交班班次岗位不一致")
            site = self._site(connection, from_shift["site_id"])
            self._check_org(actor, site)
            now = self._iso(self._now())

            def create() -> tuple[str, str, dict[str, Any]]:
                transferred = 0
                retained = 0
                items = connection.execute(
                    "SELECT * FROM handover_items WHERE shift_id=? AND status IN ('open','in_rescue') "
                    "ORDER BY created_at, item_id",
                    (from_shift_id,),
                ).fetchall()
                for item in items:
                    if item["status"] == "in_rescue":
                        # 抢救中的事项不得被自动转移。
                        self._record_decision(
                            connection, batch_id=request_id, site_id=item["site_id"],
                            item_id=item["item_id"], from_shift_id=from_shift_id, to_shift_id=None,
                            decision="retained_rescue", reason="抢救中的事项不自动转移",
                            actor_id=actor_id, occurred_at=now)
                        retained += 1
                    else:
                        connection.execute(
                            "UPDATE handover_items SET shift_id=?, updated_at=? WHERE item_id=?",
                            (to_shift_id, now, item["item_id"]),
                        )
                        self._record_decision(
                            connection, batch_id=request_id, site_id=item["site_id"],
                            item_id=item["item_id"], from_shift_id=from_shift_id,
                            to_shift_id=to_shift_id, decision="transferred", reason=None,
                            actor_id=actor_id, occurred_at=now)
                        transferred += 1
                append_event(connection, actor_id=actor_id, action="duty.handover_performed",
                             resource_type="duty_shift", resource_id=from_shift_id,
                             detail={"to_shift_id": to_shift_id, "batch_id": request_id,
                                     "transferred": transferred, "retained_rescue": retained},
                             occurred_at=now)
                return "handover_batch", request_id, {"batch_id": request_id,
                                                      "transferred": transferred,
                                                      "retained_rescue": retained}

            receipt = self.foundation._idempotent(connection, request_id=request_id,
                                                  action="duty.perform_handover", payload=payload, create=create)
            decisions = self.list_handover_decisions(site_id=from_shift["site_id"], batch_id=request_id)
            return {"batch_id": request_id, "replayed": receipt.replayed,
                    "decisions": [decision.__dict__ for decision in decisions]}

    def transfer_item(self, *, request_id: str, actor_id: str, item_id: str, to_shift_id: str,
                      reason: str = "") -> WriteReceipt:
        """显式转移单个事项；抢救中的事项必须说明理由，且不属于自动转移。"""

        payload = {"actor_id": actor_id, "item_id": item_id, "to_shift_id": to_shift_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.foundation._require(actor, "admin", "operator")
            item = self._item(connection, item_id)
            to_shift = self._shift(connection, to_shift_id)
            if to_shift["site_id"] != item["site_id"]:
                raise ValidationError("目标班次必须属于同一站点")
            site = self._site(connection, item["site_id"])
            self._check_org(actor, site)
            if item["status"] == "closed":
                raise ConflictError("事项已关闭")
            if item["shift_id"] == to_shift_id:
                raise ValidationError("事项已在目标班次上")
            reason = str(reason).strip()
            if item["status"] == "in_rescue":
                if not reason:
                    raise ValidationError("抢救中事项转移必须说明理由")
                decision = "manual_transfer_rescue"
            else:
                decision = "transferred"
            if len(reason) > 200:
                raise ValidationError("reason 不能超过 200 个字符")
            now = self._iso(self._now())

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE handover_items SET shift_id=?, updated_at=? WHERE item_id=?",
                    (to_shift_id, now, item_id),
                )
                decision_id = self._record_decision(
                    connection, batch_id=request_id, site_id=item["site_id"], item_id=item_id,
                    from_shift_id=item["shift_id"], to_shift_id=to_shift_id, decision=decision,
                    reason=reason or None, actor_id=actor_id, occurred_at=now)
                append_event(connection, actor_id=actor_id, action="duty.handover_item_transferred",
                             resource_type="handover_item", resource_id=item_id,
                             detail={"to_shift_id": to_shift_id, "decision": decision, "reason": reason},
                             occurred_at=now)
                return "handover_decision", decision_id, {"decision_id": decision_id, "decision": decision}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="duty.transfer_item", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def current_responsible(self, *, site_id: str, position: str, at: str | None = None) -> dict[str, Any]:
        """给出指定岗位在某一时刻的当前责任人。"""

        moment = self._iso(self._parse_time(at, "at") if at else self._now())
        row = self.database.connection.execute(
            "SELECT s.*, a.display_name AS holder_name FROM duty_shifts s "
            "JOIN actors a ON a.actor_id=s.holder_actor_id "
            "WHERE s.site_id=? AND s.position=? AND s.starts_at<=? AND s.ends_at>? "
            "ORDER BY s.starts_at DESC LIMIT 1",
            (site_id, position, moment, moment),
        ).fetchone()
        if row is None:
            raise NotFoundError("当前时间没有对应岗位的在岗班次")
        return {"shift_id": row["shift_id"], "site_id": row["site_id"], "position": row["position"],
                "holder_actor_id": row["holder_actor_id"], "holder_name": row["holder_name"],
                "service_date": row["service_date"], "version": row["version"],
                "starts_at": row["starts_at"], "ends_at": row["ends_at"]}

    def disclosable_scope(self, *, site_id: str, patient_ref: str, contact_name: str) -> dict[str, Any]:
        """给出某位家属当前的可披露范围；撤回后为 none，但历史披露保留。"""

        row = self.database.connection.execute(
            "SELECT * FROM contact_authorizations WHERE site_id=? AND patient_ref=? AND contact_name=? "
            "ORDER BY granted_at DESC, authorization_id DESC LIMIT 1",
            (site_id, patient_ref, contact_name),
        ).fetchone()
        base = {"site_id": site_id, "patient_ref": patient_ref, "contact_name": contact_name}
        if row is None:
            return {**base, "status": "none", "scope": "none"}
        if row["status"] == "revoked":
            return {**base, "status": "revoked", "scope": "none", "revoked_at": row["revoked_at"]}
        return {**base, "status": "active", "scope": row["scope"],
                "authorization_id": row["authorization_id"]}

    def list_shifts(self, site_id: str, service_date: str | None = None) -> list[Shift]:
        parameters: list[Any] = [site_id]
        query = "SELECT * FROM duty_shifts WHERE site_id=?"
        if service_date:
            query += " AND service_date=?"
            parameters.append(service_date)
        query += " ORDER BY starts_at, shift_id"
        return [self._to_shift(row) for row in self.database.connection.execute(query, parameters)]

    def list_pending_notifications(self, site_id: str) -> list[Notification]:
        """列出未决联络：等待投递与等待人工办结的通知。"""

        rows = self.database.connection.execute(
            "SELECT * FROM notification_requests WHERE site_id=? AND status IN ('pending','manual_handling') "
            "ORDER BY created_at, notification_id",
            (site_id,),
        ).fetchall()
        return [self._to_notification(row) for row in rows]

    def get_notification(self, notification_id: str) -> Notification:
        row = self.database.connection.execute(
            "SELECT * FROM notification_requests WHERE notification_id=?", (notification_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("通知请求不存在")
        return self._to_notification(row)

    def list_disclosures(self, site_id: str, patient_ref: str | None = None) -> list[DisclosureEvent]:
        parameters: list[Any] = [site_id]
        query = "SELECT * FROM disclosure_events WHERE site_id=?"
        if patient_ref:
            query += " AND patient_ref=?"
            parameters.append(patient_ref)
        query += " ORDER BY disclosed_at, disclosure_id"
        return [DisclosureEvent(row["disclosure_id"], row["authorization_id"], row["notification_id"],
                                row["site_id"], row["patient_ref"], row["contact_name"], row["scope"],
                                row["channel"], row["disclosed_by"], row["disclosed_at"])
                for row in self.database.connection.execute(query, parameters)]

    def list_handover_items(self, site_id: str, shift_id: str | None = None) -> list[HandoverItem]:
        parameters: list[Any] = [site_id]
        query = "SELECT * FROM handover_items WHERE site_id=?"
        if shift_id:
            query += " AND shift_id=?"
            parameters.append(shift_id)
        query += " ORDER BY created_at, item_id"
        return [HandoverItem(row["item_id"], row["site_id"], row["shift_id"], row["patient_ref"],
                             row["category"], row["summary"], row["status"], row["created_by"],
                             row["created_at"], row["updated_at"])
                for row in self.database.connection.execute(query, parameters)]

    def list_handover_decisions(self, site_id: str, batch_id: str | None = None) -> list[HandoverDecision]:
        parameters: list[Any] = [site_id]
        query = "SELECT * FROM handover_decisions WHERE site_id=?"
        if batch_id:
            query += " AND batch_id=?"
            parameters.append(batch_id)
        query += " ORDER BY decided_at, rowid"
        return [HandoverDecision(row["decision_id"], row["batch_id"], row["site_id"], row["item_id"],
                                 row["from_shift_id"], row["to_shift_id"], row["decision"],
                                 row["reason"], row["decided_by"], row["decided_at"])
                for row in self.database.connection.execute(query, parameters)]

    def _to_shift(self, row) -> Shift:
        return Shift(row["shift_id"], row["site_id"], row["position"], row["holder_actor_id"],
                     row["starts_at"], row["ends_at"], row["service_date"], row["timezone_name"],
                     row["version"], row["created_at"])

    def _to_notification(self, row) -> Notification:
        return Notification(row["notification_id"], row["site_id"], row["patient_ref"],
                            row["authorization_id"], row["scope"], row["summary"],
                            row["window_start"], row["window_end"], row["status"], row["attempts"],
                            row["max_attempts"], row["manual_due_at"], row["created_by"],
                            row["created_at"], row["updated_at"])
