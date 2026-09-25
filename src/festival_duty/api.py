"""急诊节日值守与家属联络模块的 HTTP/JSON 边界。

路由在 /duty/ 前缀下提供本模块接口，其余路径回退到基础层路由，
因此一个进程即可同时提供基础层与领域模块能力。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from festival_foundation.api import route as foundation_route
from festival_foundation.errors import DomainError, ValidationError
from festival_foundation.service import DomainService
from festival_foundation.storage import Database

from .service import DutyService


def _required(query: dict[str, list[str]], name: str) -> str:
    value = query.get(name, [""])[0]
    if not value:
        raise ValidationError(f"{name} 不能为空")
    return value


def _optional(query: dict[str, list[str]], name: str) -> str | None:
    return query.get(name, [None])[0]


def route_duty(service: DutyService, method: str, path: str, body: dict[str, Any],
               headers: dict[str, str]) -> tuple[int, dict[str, Any]] | None:
    """处理 /duty/ 前缀的请求；路径不属于本模块时返回 None。"""

    parsed = urlparse(path)
    if not parsed.path.startswith("/duty/"):
        return None
    actor_id = headers.get("X-Actor-Id", "")
    query = parse_qs(parsed.query)
    try:
        if method == "POST" and parsed.path == "/duty/shifts":
            receipt = service.create_shift(actor_id=actor_id, **body)
            return (200 if receipt.replayed else 201), receipt.__dict__
        if method == "GET" and parsed.path == "/duty/shifts":
            site_id = _required(query, "site_id")
            items = service.list_shifts(site_id, _optional(query, "service_date"))
            return 200, {"items": [item.__dict__ for item in items]}
        if method == "GET" and parsed.path == "/duty/shifts/current":
            return 200, service.current_responsible(site_id=_required(query, "site_id"),
                                                    position=_required(query, "position"),
                                                    at=_optional(query, "at"))
        if method == "POST" and parsed.path == "/duty/qualifications":
            receipt = service.grant_qualification(actor_id=actor_id, **body)
            return (200 if receipt.replayed else 201), receipt.__dict__
        if method == "POST" and parsed.path == "/duty/qualifications/revoke":
            receipt = service.revoke_qualification(actor_id=actor_id, **body)
            return (200 if receipt.replayed else 201), receipt.__dict__
        if method == "POST" and parsed.path == "/duty/swaps":
            receipt = service.request_swap(actor_id=actor_id, **body)
            return (200 if receipt.replayed else 201), receipt.__dict__
        if method == "POST" and parsed.path == "/duty/swaps/confirm":
            return 200, service.confirm_swap(actor_id=actor_id, **body)
        if method == "POST" and parsed.path == "/duty/swaps/decline":
            return 200, service.decline_swap(actor_id=actor_id, **body)
        if method == "POST" and parsed.path == "/duty/authorizations":
            receipt = service.grant_authorization(actor_id=actor_id, **body)
            return (200 if receipt.replayed else 201), receipt.__dict__
        if method == "POST" and parsed.path == "/duty/authorizations/revoke":
            receipt = service.revoke_authorization(actor_id=actor_id, **body)
            return (200 if receipt.replayed else 201), receipt.__dict__
        if method == "GET" and parsed.path == "/duty/authorizations/scope":
            return 200, service.disclosable_scope(site_id=_required(query, "site_id"),
                                                  patient_ref=_required(query, "patient_ref"),
                                                  contact_name=_required(query, "contact_name"))
        if method == "POST" and parsed.path == "/duty/notifications":
            receipt = service.request_notification(actor_id=actor_id, **body)
            return (200 if receipt.replayed else 201), receipt.__dict__
        if method == "POST" and parsed.path == "/duty/notifications/deliver":
            return 200, service.deliver_notification(actor_id=actor_id, **body)
        if method == "POST" and parsed.path == "/duty/notifications/resolve":
            return 200, service.resolve_manual_notification(actor_id=actor_id, **body)
        if method == "GET" and parsed.path == "/duty/notifications/pending":
            items = service.list_pending_notifications(_required(query, "site_id"))
            return 200, {"items": [item.__dict__ for item in items]}
        if method == "GET" and parsed.path == "/duty/disclosures":
            items = service.list_disclosures(_required(query, "site_id"),
                                             _optional(query, "patient_ref"))
            return 200, {"items": [item.__dict__ for item in items]}
        if method == "POST" and parsed.path == "/duty/handover-items":
            receipt = service.create_handover_item(actor_id=actor_id, **body)
            return (200 if receipt.replayed else 201), receipt.__dict__
        if method == "GET" and parsed.path == "/duty/handover-items":
            items = service.list_handover_items(_required(query, "site_id"),
                                                _optional(query, "shift_id"))
            return 200, {"items": [item.__dict__ for item in items]}
        if method == "POST" and parsed.path == "/duty/handover-items/status":
            return 200, service.set_item_status(actor_id=actor_id, **body)
        if method == "POST" and parsed.path == "/duty/handovers":
            return 200, service.perform_handover(actor_id=actor_id, **body)
        if method == "POST" and parsed.path == "/duty/handovers/transfer-item":
            receipt = service.transfer_item(actor_id=actor_id, **body)
            return (200 if receipt.replayed else 201), receipt.__dict__
        if method == "GET" and parsed.path == "/duty/handovers/decisions":
            items = service.list_handover_decisions(_required(query, "site_id"),
                                                    _optional(query, "batch_id"))
            return 200, {"items": [item.__dict__ for item in items]}
        if method == "POST" and parsed.path == "/duty/maintenance/sweep":
            return 200, service.sweep(actor_id=actor_id)
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def create_router(foundation: DomainService,
                  duty: DutyService) -> Callable[[str, str, dict[str, Any] | None, dict[str, str] | None],
                                                 tuple[int, dict[str, Any]]]:
    """组合本模块与基础层的路由。"""

    def router(method: str, path: str, body: dict[str, Any] | None = None,
               headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        handled = route_duty(duty, method, path, body or {}, headers or {})
        if handled is not None:
            return handled
        return foundation_route(foundation, method, path, body, headers)

    return router


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为组合路由调用。"""

    router: Callable[[str, str, dict[str, Any] | None, dict[str, str] | None],
                     tuple[int, dict[str, Any]]]

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = type(self).router(self.command, self.path, body,
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
    """启动同时提供基础层与急诊值守模块的 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动急诊节日值守与家属联络服务")
    parser.add_argument("--database", default="duty.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    foundation = DomainService(database)
    duty = DutyService(foundation)
    recovered = duty.recover()
    print(json.dumps({"recovered": recovered}, ensure_ascii=False, sort_keys=True))
    Handler.router = create_router(foundation, duty)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
