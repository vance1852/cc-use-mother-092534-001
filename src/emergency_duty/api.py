"""急诊值守模块的 HTTP/JSON 边界。

复用基础层路由与鉴权约定（X-Actor-Id、request_id 幂等），新增急诊
业务路由；启动时同时拉起后台维护线程，进程重启后继续处理未决事项。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from festival_foundation.api import route as foundation_route
from festival_foundation.errors import DomainError, ValidationError
from festival_foundation.service import DomainService
from festival_foundation.storage import Database

from .scheduler import MaintenanceWorker
from .service import EmergencyDutyService


def ed_route(service: EmergencyDutyService, method: str, path: str, body: dict[str, Any],
             headers: dict[str, str]) -> tuple[int, dict[str, Any]] | None:
    """处理急诊模块路由；不认识的路径返回 None 交给基础层。"""

    parsed = urlparse(path)
    segments = [s for s in parsed.path.split("/") if s]
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")

    def q(name: str, default: str | None = None) -> str | None:
        return query.get(name, [default])[0]

    try:
        if method == "POST" and parsed.path == "/ed/patients":
            return _receipt(service.register_patient(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/ed/patient-state":
            return _receipt(service.update_patient_state(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/ed/qualifications":
            return _receipt(service.grant_qualification(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/ed/qualifications/revoke":
            return _receipt(service.revoke_qualification(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/ed/qualifications":
            site_id = q("site_id", "")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, {"items": [q_obj.__dict__ for q_obj in
                                  service.list_qualifications(site_id, q("actor_id"))]}
        if method == "POST" and parsed.path == "/ed/shifts":
            return _receipt(service.publish_shift(actor_id=actor_id, **body))
        if method == "GET" and len(segments) == 3 and segments[:2] == ["ed", "shifts"]:
            return 200, _shift(service.get_shift(segments[2]))
        if method == "POST" and parsed.path == "/ed/handover-items":
            return _receipt(service.add_handover_item(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/ed/handover-items/complete":
            return _receipt(service.complete_handover_item(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/ed/handover-items/transfer":
            return _receipt(service.transfer_handover_item(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/ed/handover-items":
            shift_id = q("shift_id", "")
            if not shift_id:
                raise ValidationError("shift_id 不能为空")
            return 200, {"items": [i.__dict__ for i in
                                   service.list_handover_items(shift_id, q("status"))]}
        if method == "POST" and parsed.path == "/ed/handovers":
            return _receipt(service.propose_handover(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/ed/handovers/confirm":
            return _receipt(service.confirm_handover(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/ed/handovers/reject":
            return _receipt(service.reject_handover(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/ed/handovers/retry":
            return _receipt(service.retry_handover(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/ed/handovers":
            site_id = q("site_id", "")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, {"items": [h.__dict__ for h in
                                   service.list_handovers(site_id, q("status"))]}
        if method == "GET" and len(segments) == 3 and segments[:2] == ["ed", "handovers"]:
            return 200, service.get_handover(segments[2]).__dict__
        if method == "POST" and parsed.path == "/ed/authorizations":
            return _receipt(service.grant_authorization(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/ed/authorizations/revoke":
            return _receipt(service.revoke_authorization(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/ed/authorizations":
            patient_id = q("patient_id", "")
            if not patient_id:
                raise ValidationError("patient_id 不能为空")
            include = q("include_revoked", "true") != "false"
            return 200, {"items": [a.__dict__ for a in
                                   service.list_authorizations(patient_id, include)]}
        if method == "GET" and len(segments) == 4 and segments[:2] == ["ed", "authorizations"] \
                and segments[3] == "access":
            return 200, {"items": service.list_access_history(segments[2])}
        if method == "POST" and parsed.path == "/ed/disclosures":
            return _receipt(service.log_disclosure(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/ed/notifications":
            return _receipt(service.submit_notification(actor_id=actor_id, **body))
        if method == "GET" and len(segments) == 3 and segments[:2] == ["ed", "notifications"]:
            return 200, service.get_notification(segments[2]).__dict__
        if method == "POST" and parsed.path == "/ed/manual-tasks/resolve":
            return _receipt(service.resolve_manual_task(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/ed/manual-tasks":
            site_id = q("site_id", "")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, {"items": [t.__dict__ for t in
                                   service.list_manual_tasks(site_id, q("status"))]}
        if method == "POST" and parsed.path == "/ed/maintenance/run":
            return 200, {"status": "ok", **service.run_maintenance()}
        if method == "GET" and parsed.path == "/ed/responsibility":
            site_id = q("site_id", "")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, service.current_responsibility(site_id)
        if method == "GET" and parsed.path == "/ed/pending-contacts":
            site_id = q("site_id", "")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, service.list_pending_contacts(site_id)
        if method == "GET" and parsed.path == "/ed/duty":
            site_id = q("site_id", "")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, service.duty_snapshot(site_id).__dict__
        return None
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _receipt(receipt: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.get("replayed") else 201), receipt


def _shift(shift) -> dict[str, Any]:
    data = shift.__dict__.copy()
    data["assignments"] = [a.__dict__ for a in shift.assignments]
    return data


def combined_route(foundation: DomainService, emergency: EmergencyDutyService, method: str,
                   path: str, body: dict[str, Any] | None,
                   headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """先派给急诊模块，未命中再走基础层。"""

    headers = headers or {}
    body = body or {}
    result = ed_route(emergency, method, path, body, headers)
    if result is not None:
        return result
    return foundation_route(foundation, method, path, body, headers)


class Handler(BaseHTTPRequestHandler):
    foundation: DomainService
    emergency: EmergencyDutyService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = combined_route(
            self.foundation, self.emergency, self.command, self.path, body,
            {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="启动急诊节日值守与家属联络服务")
    parser.add_argument("--database", default="emergency_duty.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--maintenance-interval", type=float, default=30.0)
    args = parser.parse_args()
    database = Database(args.database)
    foundation = DomainService(database)
    emergency = EmergencyDutyService(database)
    worker = MaintenanceWorker(emergency, interval_seconds=args.maintenance_interval)
    Handler.foundation = foundation
    Handler.emergency = emergency
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    worker.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop(timeout=5)
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
