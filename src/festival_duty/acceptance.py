"""运行急诊节日值守与家属联络模块的离线端到端验收。

场景覆盖：跨午夜班次归属、双方确认的临时换岗、授权与披露、授权撤回、
失败投递转人工处置、抢救中事项保留，以及进程重启后的续办。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from festival_foundation.clock import FixedClock
from festival_foundation.errors import PermissionDenied
from festival_foundation.service import DomainService
from festival_foundation.storage import Database

from .gateway import DeliveryFailure
from .service import DutyService


NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)  # 中秋夜 20:00（Asia/Shanghai）


class _FailingGateway:
    """模拟持续故障的投递渠道。"""

    def deliver(self, *, notification, authorization, summary):
        raise DeliveryFailure("模拟渠道故障")


def _bootstrap(foundation: DomainService) -> None:
    foundation.register_organization(request_id="acc-org", actor_id="bootstrap",
                                     organization_id="org-er", name="中秋急诊值守中心")
    foundation.register_actor(request_id="acc-admin", actor_id="bootstrap", new_actor_id="er-admin",
                              display_name="值班管理员", role="admin", organization_id="org-er")
    foundation.register_actor(request_id="acc-dr-a", actor_id="er-admin", new_actor_id="dr-a",
                              display_name="张医生", role="operator", organization_id="org-er")
    foundation.register_actor(request_id="acc-dr-b", actor_id="er-admin", new_actor_id="dr-b",
                              display_name="李医生", role="operator", organization_id="org-er")
    foundation.register_actor(request_id="acc-nurse", actor_id="er-admin", new_actor_id="head-nurse",
                              display_name="护士长", role="reviewer", organization_id="org-er")
    foundation.register_site(request_id="acc-site", actor_id="er-admin", site_id="site-er",
                             organization_id="org-er", name="急诊一区", timezone_name="Asia/Shanghai")


def run() -> dict[str, object]:
    """执行完整业务链并返回验收结果。"""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "duty.sqlite3"
        database = Database(path)
        foundation = DomainService(database, FixedClock(NOW))
        _bootstrap(foundation)
        duty = DutyService(foundation)

        # 岗位资格与跨午夜班次：20:00 至次日 08:00（院区时区），归属中秋当日。
        duty.grant_qualification(request_id="acc-qual-a", actor_id="er-admin", target_actor_id="dr-a",
                                 position="rescue", expires_at="2026-10-10T00:00:00Z",
                                 qualification_id="qual-a")
        duty.grant_qualification(request_id="acc-qual-b", actor_id="er-admin", target_actor_id="dr-b",
                                 position="rescue", expires_at="2026-10-10T00:00:00Z",
                                 qualification_id="qual-b")
        duty.create_shift(request_id="acc-shift-night", actor_id="er-admin", site_id="site-er",
                          position="rescue", holder_actor_id="dr-a",
                          starts_at="2026-09-25T12:00:00Z", ends_at="2026-09-26T00:00:00Z",
                          shift_id="shift-night")
        duty.create_shift(request_id="acc-shift-next", actor_id="er-admin", site_id="site-er",
                          position="rescue", holder_actor_id="dr-b",
                          starts_at="2026-09-26T00:00:00Z", ends_at="2026-09-26T12:00:00Z",
                          shift_id="shift-next")
        shift_night = duty.list_shifts("site-er", service_date="2026-09-25")

        # 临时换岗：双方确认后生效，班次版本递增。
        duty.request_swap(request_id="acc-swap", actor_id="dr-a", shift_id="shift-night",
                          to_actor_id="dr-b", reason="跨科支援", swap_id="swap-1")
        duty.confirm_swap(actor_id="dr-a", swap_id="swap-1")
        swap = duty.confirm_swap(actor_id="dr-b", swap_id="swap-1")
        responsible = duty.current_responsible(site_id="site-er", position="rescue")

        # 家属联络授权与一次成功投递。
        duty.grant_authorization(request_id="acc-auth-1", actor_id="dr-a", site_id="site-er",
                                 patient_ref="patient-001", contact_name="王女士",
                                 contact_channel="phone", scope="condition_summary",
                                 authorization_id="auth-1")
        duty.request_notification(request_id="acc-notif-1", actor_id="dr-a", site_id="site-er",
                                  patient_ref="patient-001", authorization_id="auth-1",
                                  scope="condition_summary", summary="患者生命体征平稳，继续留观。",
                                  window_start="2026-09-25T08:00:00Z",
                                  window_end="2026-09-25T16:00:00Z",
                                  notification_id="notif-1")
        delivered = duty.deliver_notification(actor_id="dr-a", notification_id="notif-1")

        # 授权撤回：立即阻止新的披露，历史披露保留。
        duty.revoke_authorization(request_id="acc-revoke-1", actor_id="er-admin",
                                  authorization_id="auth-1")
        scope_after_revoke = duty.disclosable_scope(site_id="site-er", patient_ref="patient-001",
                                                    contact_name="王女士")
        blocked = False
        try:
            duty.request_notification(request_id="acc-notif-blocked", actor_id="dr-a", site_id="site-er",
                                      patient_ref="patient-001", authorization_id="auth-1",
                                      scope="condition_summary", summary="撤回后不应再披露。",
                                      window_start="2026-09-25T08:00:00Z",
                                      window_end="2026-09-25T16:00:00Z")
        except PermissionDenied:
            blocked = True

        # 交班：普通事项转移，抢救中的事项自动保留。
        duty.create_handover_item(request_id="acc-item-1", actor_id="dr-b", site_id="site-er",
                                  shift_id="shift-night", patient_ref="patient-001",
                                  category="observation", summary="留观患者，夜间复查血象。",
                                  item_id="item-1")
        duty.create_handover_item(request_id="acc-item-2", actor_id="dr-b", site_id="site-er",
                                  shift_id="shift-night", patient_ref="patient-002",
                                  category="rescue", summary="抢救中，持续心电监护。",
                                  in_rescue=True, item_id="item-2")
        handover = duty.perform_handover(request_id="acc-handover", actor_id="dr-b",
                                         from_shift_id="shift-night", to_shift_id="shift-next")

        # 失败投递进入有期限的人工处置，由护士长人工办结。
        duty.grant_authorization(request_id="acc-auth-2", actor_id="dr-b", site_id="site-er",
                                 patient_ref="patient-002", contact_name="李先生",
                                 contact_channel="phone", scope="identity_only",
                                 authorization_id="auth-2")
        duty.request_notification(request_id="acc-notif-2", actor_id="dr-b", site_id="site-er",
                                  patient_ref="patient-002", authorization_id="auth-2",
                                  scope="identity_only", summary="请家属尽快到院。",
                                  window_start="2026-09-25T08:00:00Z",
                                  window_end="2026-09-26T00:00:00Z",
                                  notification_id="notif-2", max_attempts=2)
        failing = DutyService(foundation, gateway=_FailingGateway())
        failing.deliver_notification(actor_id="dr-b", notification_id="notif-2")
        manual = failing.deliver_notification(actor_id="dr-b", notification_id="notif-2")
        resolved = duty.resolve_manual_notification(actor_id="head-nurse", notification_id="notif-2",
                                                    outcome="delivered", note="已电话联系到家属")

        # 一条窗口已过的通知留到“进程重启”之后继续处理。
        duty.request_notification(request_id="acc-notif-3", actor_id="dr-b", site_id="site-er",
                                  patient_ref="patient-002", authorization_id="auth-2",
                                  scope="identity_only", summary="留观床位调整，请知悉。",
                                  window_start="2026-09-25T09:00:00Z",
                                  window_end="2026-09-25T10:00:00Z",
                                  notification_id="notif-3")
        disclosures_before_restart = len(duty.list_disclosures("site-er"))
        database.close()

        # 模拟进程重启：同一数据库文件上的新服务实例继续处理未完成事项。
        database2 = Database(path)
        foundation2 = DomainService(database2, FixedClock(NOW))
        duty2 = DutyService(foundation2)
        recovered = duty2.recover()
        pending_after_restart = [item.__dict__ for item in duty2.list_pending_notifications("site-er")]
        valid, event_count = foundation2.verify_audit()
        result = {
            "status": "ok",
            "service_date": shift_night[0].service_date if shift_night else None,
            "swap_status": swap["status"],
            "shift_version": responsible["version"],
            "current_holder": responsible["holder_actor_id"],
            "delivered_status": delivered["status"],
            "scope_after_revoke": scope_after_revoke["scope"],
            "blocked_after_revoke": blocked,
            "handover_decisions": [d["decision"] for d in handover["decisions"]],
            "manual_status": manual["status"],
            "resolved_status": resolved["status"],
            "disclosures": disclosures_before_restart,
            "recovered": recovered,
            "pending_after_restart": len(pending_after_restart),
            "audit_valid": valid,
            "audit_events": event_count,
        }
        database2.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
