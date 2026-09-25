"""急诊节日值守与家属联络模块的离线端到端验收。

覆盖：班次版本与院区时区归属、岗位资格门禁、双方确认换岗、抢救事项
阻塞、授权撤回语义、通知安全重放/内容冲突、有限重试与人工处置期限、
进程重启后继续处理，以及哈希审计链完整性。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from festival_foundation.service import DomainService
from festival_foundation.storage import Database

from .errors import DeliveryConflictError, HandoverBlockedError
from .service import EmergencyDutyService
from .transports import FailingTransport, FlakyTransport, RecordingTransport


class MutableClock:
    """验收用可调时钟。"""

    def __init__(self, value: datetime) -> None:
        self.value = value.astimezone(timezone.utc)

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("时间必须包含时区")
        self.value = value.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self.value


def _bootstrap(database: Database, clock: MutableClock) -> None:
    foundation = DomainService(database, clock)
    foundation.register_organization(request_id="org", actor_id="bootstrap",
                                     organization_id="o1", name="示范医院")
    foundation.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-1",
                              display_name="值班管理员", role="admin", organization_id="o1")
    foundation.register_actor(request_id="zhang", actor_id="admin-1", new_actor_id="zhang",
                              display_name="张医生", role="operator", organization_id="o1")
    foundation.register_actor(request_id="wang", actor_id="admin-1", new_actor_id="wang",
                              display_name="王医生", role="operator", organization_id="o1")
    foundation.register_actor(request_id="li", actor_id="admin-1", new_actor_id="li",
                              display_name="李护士", role="operator", organization_id="o1")
    foundation.register_actor(request_id="chen", actor_id="admin-1", new_actor_id="chen",
                              display_name="陈护士", role="operator", organization_id="o1")
    foundation.register_site(request_id="site", actor_id="admin-1", site_id="s1",
                             organization_id="o1", name="总院急诊", timezone_name="Asia/Shanghai")


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "emergency.sqlite3"
        database = Database(db_path)
        clock = MutableClock(datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc))  # 本地 22:00
        _bootstrap(database, clock)
        recorder = RecordingTransport()
        service = EmergencyDutyService(database, clock, recorder)
        flaky = EmergencyDutyService(database, clock, FlakyTransport(1))
        failing = EmergencyDutyService(database, clock, FailingTransport())

        # 1) 岗位资格：张医生、李护士、陈护士具备资质；王医生暂不具备。
        service.grant_qualification(request_id="q-zhang", actor_id="admin-1",
                                    qualification_id="qz", site_id="s1", target_actor_id="zhang",
                                    position_code="rescue_lead",
                                    valid_from="2026-09-25T00:00Z",
                                    valid_until="2026-09-27T00:00Z")
        service.grant_qualification(request_id="q-li", actor_id="admin-1", qualification_id="ql",
                                    site_id="s1", target_actor_id="li", position_code="observation",
                                    valid_from="2026-09-25T00:00Z",
                                    valid_until="2026-09-27T00:00Z")
        service.grant_qualification(request_id="q-chen", actor_id="admin-1",
                                    qualification_id="qc", site_id="s1", target_actor_id="chen",
                                    position_code="observation",
                                    valid_from="2026-09-25T00:00Z",
                                    valid_until="2026-09-27T00:00Z")

        # 2) 班次版本：跨午夜夜班 21:00->次日21:00 本地，按院区时区归属 9/25；
        #    第二版发布后第一版自动停用。
        service.publish_shift(request_id="shift-v1", actor_id="admin-1", shift_id="sh-v1",
                              site_id="s1", starts_at="2026-09-25T13:00Z",
                              ends_at="2026-09-26T13:00Z",
                              assignments=[{"position_code": "rescue_lead", "holder_actor_id": "zhang"},
                                           {"position_code": "observation", "holder_actor_id": "li"}])
        v2 = service.publish_shift(request_id="shift-v2", actor_id="admin-1", shift_id="sh-v2",
                                   site_id="s1", starts_at="2026-09-25T13:00Z",
                                   ends_at="2026-09-26T13:00Z",
                                   assignments=[{"position_code": "rescue_lead", "holder_actor_id": "zhang"},
                                                {"position_code": "observation", "holder_actor_id": "li"}])
        assert v2["version"] == 2 and v2["shift_date"] == "2026-09-25"
        assert service.get_shift("sh-v1").active is False

        # 3) 患者与交班事项：p1 抢救中关键事项；p2 抢救中关键事项用于转移测试。
        service.register_patient(request_id="p1", actor_id="zhang", patient_id="p1",
                                 site_id="s1", display_name="患者甲", care_state="rescue")
        service.register_patient(request_id="p2", actor_id="li", patient_id="p2",
                                 site_id="s1", display_name="患者乙", care_state="rescue")
        item = service.add_handover_item(request_id="item-1", actor_id="zhang", shift_id="sh-v2",
                                         position_code="rescue_lead", kind="rescue",
                                         summary="抢救进行中，用药待续", patient_id="p1",
                                         critical=True)
        item2 = service.add_handover_item(request_id="item-2", actor_id="li", shift_id="sh-v2",
                                          position_code="observation", kind="observation",
                                          summary="留观补液", patient_id="p2", critical=True)

        # 4) 临时换岗：提议 -> 双方确认；王医生无资格时生效被阻止。
        proposal = service.propose_handover(request_id="ho", actor_id="zhang", shift_id="sh-v2",
                                            position_code="rescue_lead", incoming_actor_id="wang",
                                            reason="临时跨区支援")
        assert proposal["status"] == "proposed"
        service.confirm_handover(request_id="ho-c-out", actor_id="zhang",
                                 handover_id=proposal["handover_id"])
        blocked = service.confirm_handover(request_id="ho-c-in-1", actor_id="wang",
                                           handover_id=proposal["handover_id"])
        assert blocked["status"] == "blocked"
        assert blocked["blocked_reason"] == "qualification_invalid"

        # 补办资格后仍因抢救关键事项阻塞；患者转为留观后换岗生效。
        service.grant_qualification(request_id="q-wang", actor_id="admin-1",
                                    qualification_id="qw", site_id="s1", target_actor_id="wang",
                                    position_code="rescue_lead",
                                    valid_from="2026-09-25T00:00Z",
                                    valid_until="2026-09-27T00:00Z")
        still = service.retry_handover(request_id="ho-retry-1", actor_id="wang",
                                       handover_id=proposal["handover_id"])
        assert still["blocked_reason"] == "critical_rescue_items_open"
        service.update_patient_state(request_id="p1-obs", actor_id="zhang", patient_id="p1",
                                     care_state="observation")
        effective = service.retry_handover(request_id="ho-retry-2", actor_id="wang",
                                           handover_id=proposal["handover_id"])
        assert effective["status"] == "effective"
        holders = {a["position_code"]: a["actor_id"]
                   for a in service.current_responsibility("s1")["responsible_actors"]}
        assert holders["rescue_lead"] == "wang"

        # 5) 抢救中的事项不得自动/手动转移；脱离抢救后可跨科转移。
        try:
            service.transfer_handover_item(request_id="tr-block", actor_id="li",
                                           item_id=item2["item_id"], to_shift_id="sh-v2",
                                           to_position_code="rescue_lead")
            raise AssertionError("抢救事项转移应当被阻止")
        except HandoverBlockedError:
            pass
        service.update_patient_state(request_id="p2-obs", actor_id="li", patient_id="p2",
                                     care_state="observation")
        transferred = service.transfer_handover_item(request_id="tr-ok", actor_id="li",
                                                     item_id=item2["item_id"], to_shift_id="sh-v2",
                                                     to_position_code="rescue_lead")
        assert transferred["status"] == "transferred"

        # 6) 联络授权：母亲（窗口 08:00-21:00 本地）、父亲（全天）、叔叔（全天）。
        service.grant_authorization(request_id="a-mother", actor_id="zhang", patient_id="p1",
                                    contact_name="母亲", contact_channel="mom-phone",
                                    scope=["condition", "plan"], window_start="08:00",
                                    window_end="21:00")
        service.grant_authorization(request_id="a-father", actor_id="zhang", patient_id="p1",
                                    contact_name="父亲", contact_channel="dad-phone",
                                    scope=["condition"])
        service.grant_authorization(request_id="a-uncle", actor_id="zhang", patient_id="p1",
                                    contact_name="叔叔", contact_channel="uncle-phone",
                                    scope=["administrative"])
        auths = {a.contact_name: a for a in service.list_authorizations("p1")}

        # 当前本地 22:00，已在母亲窗口外：通知排队等待到次日 08:00 本地（00:00Z）。
        submit = service.submit_notification(request_id="n1", actor_id="zhang", site_id="s1",
                                             patient_id="p1",
                                             authorization_id=auths["母亲"].authorization_id,
                                             channel="sms", subject="病情简报",
                                             content="已转为留观观察")
        replay = service.submit_notification(request_id="n1", actor_id="zhang", site_id="s1",
                                             patient_id="p1",
                                             authorization_id=auths["母亲"].authorization_id,
                                             channel="sms", subject="病情简报",
                                             content="已转为留观观察")
        assert submit["replayed"] is False and replay["replayed"] is True
        try:
            service.submit_notification(request_id="n1", actor_id="zhang", site_id="s1",
                                        patient_id="p1",
                                        authorization_id=auths["母亲"].authorization_id,
                                        channel="sms", subject="病情简报（改）",
                                        content="不同内容")
            raise AssertionError("相同请求编号不同内容必须冲突")
        except DeliveryConflictError:
            pass
        outcome = service.process_due_deliveries()
        assert outcome["waiting_window"] == 1

        # 7) 推进到次日 08:00 本地，窗口打开投递成功并写入披露历史。
        clock.set(datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc))
        assert service.run_maintenance()["delivered"] == 1
        assert service.get_notification("n1").status == "delivered"
        history = service.list_access_history(auths["母亲"].authorization_id)
        assert len(history) == 1 and history[0]["notification_request_id"] == "n1"

        # 8) 通道一次故障：退避重试后成功，不进人工队列。
        flaky.submit_notification(request_id="n2", actor_id="zhang", site_id="s1", patient_id="p1",
                                  authorization_id=auths["父亲"].authorization_id,
                                  channel="sms", subject="父亲简报", content="情况稳定")
        assert flaky.run_maintenance()["retried"] == 1
        clock.set(datetime(2026, 9, 26, 0, 2, tzinfo=timezone.utc))
        assert flaky.run_maintenance()["delivered"] == 1

        # 9) 持续故障：3 次尝试后进入有期限人工处置，不无限重试。
        failing.submit_notification(request_id="n3", actor_id="zhang", site_id="s1",
                                    patient_id="p1",
                                    authorization_id=auths["叔叔"].authorization_id,
                                    channel="sms", subject="手续通知", content="需补办手续")
        assert failing.run_maintenance()["retried"] == 1
        clock.set(datetime(2026, 9, 26, 0, 7, tzinfo=timezone.utc))
        assert failing.run_maintenance()["retried"] == 1
        clock.set(datetime(2026, 9, 26, 0, 13, tzinfo=timezone.utc))
        result = failing.run_maintenance()
        assert result["manual"] == 1 and result["retried"] == 0
        tasks = service.list_manual_tasks("s1", status="open")
        assert len(tasks) == 1 and tasks[0].request_id == "n3"
        assert failing.get_notification("n3").attempt_count == 3
        # 人工处置期限内可结办；这里推进到超期后自动关闭。
        clock.set(datetime(2026, 9, 26, 3, 0, tzinfo=timezone.utc))
        assert service.expire_manual_tasks() == 1
        assert service.list_manual_tasks("s1", status="expired")[0].request_id == "n3"

        # 10) 撤回母亲授权：立即阻止继续披露，但历史访问保留。
        service.revoke_authorization(request_id="rev-mother", actor_id="zhang",
                                     authorization_id=auths["母亲"].authorization_id)
        scope_now = {item["contact_name"] for item in service.disclosure_scope("p1")}
        assert "母亲" not in scope_now
        from festival_foundation.errors import PermissionDenied
        try:
            service.log_disclosure(request_id="d1", actor_id="zhang",
                                   authorization_id=auths["母亲"].authorization_id,
                                   summary="撤回后的口头说明")
            raise AssertionError("撤回后必须阻止披露")
        except PermissionDenied:
            pass
        assert len(service.list_access_history(auths["母亲"].authorization_id)) == 1

        # 11) 撤回立即阻断尚未发出的通知。
        service.submit_notification(request_id="n4", actor_id="zhang", site_id="s1",
                                    patient_id="p1",
                                    authorization_id=auths["叔叔"].authorization_id,
                                    channel="sms", subject="阻断测试", content="不应发出")
        service.revoke_authorization(request_id="rev-uncle", actor_id="zhang",
                                     authorization_id=auths["叔叔"].authorization_id)
        # 撤回事务内立即阻断，无需等待调度周期；再跑维护也不会重复处理它。
        assert service.get_notification("n4").status == "blocked"
        blocked_outcome = service.run_maintenance()
        assert blocked_outcome["blocked"] == 0
        assert not any(t.request_id == "n4" for t in service.list_manual_tasks("s1"))

        # 12) 进程重启：落库状态继续，未决失败通知在新进程里继续重试直至人工处置；
        #     换岗决定、责任人、审计链都还在。
        before_restart = service.list_handovers("s1")
        database.close()
        database = Database(db_path)
        reopened = EmergencyDutyService(database, clock, FailingTransport())
        holders = {a["position_code"]: a["actor_id"]
                   for a in reopened.current_responsibility("s1")["responsible_actors"]}
        assert holders["rescue_lead"] == "wang"
        assert len(reopened.list_handovers("s1")) == len(before_restart)
        # 叔叔授权已撤回，改用父亲授权验证重启后的投递链。
        father = [a for a in reopened.list_authorizations("p1") if a.contact_name == "父亲"][0]
        reopened.submit_notification(request_id="n5", actor_id="zhang", site_id="s1",
                                     patient_id="p1", authorization_id=father.authorization_id,
                                     channel="sms", subject="重启后通知", content="继续处理")
        reopened.run_maintenance()
        clock.set(datetime(2026, 9, 26, 3, 2, tzinfo=timezone.utc))
        reopened.run_maintenance()
        clock.set(datetime(2026, 9, 26, 3, 8, tzinfo=timezone.utc))
        reopened.run_maintenance()
        assert reopened.get_notification("n5").status == "failed"
        assert any(t.request_id == "n5" for t in reopened.list_manual_tasks("s1", status="open"))

        # 13) 后台聚合视图与审计链。
        snapshot = reopened.duty_snapshot("s1")
        pending_ids = {p.get("request_id") for p in snapshot.pending_contacts}
        assert "n5" in pending_ids
        valid, event_count = DomainService(database, clock).verify_audit()
        database.close()
        return {"status": "ok" if valid else "audit_broken", "audit_valid": valid,
                "audit_events": event_count, "shift_date": v2["shift_date"],
                "responsible_lead": holders["rescue_lead"],
                "manual_tasks_open": 1, "delivered": ("n1", "n2"),
                "blocked": "n4"}


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
