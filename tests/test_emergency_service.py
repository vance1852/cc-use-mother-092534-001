import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from festival_foundation.errors import ConflictError, PermissionDenied, ValidationError
from festival_foundation.service import DomainService
from festival_foundation.storage import Database

from emergency_duty.clock_support import SteppingClock
from emergency_duty.errors import DeliveryConflictError, HandoverBlockedError
from emergency_duty.service import EmergencyDutyService
from emergency_duty.timeutil import next_window_open, parse_utc, shift_local_date, within_window
from emergency_duty.transports import FailingTransport, RecordingTransport


class EmergencyServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = SteppingClock(datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc))
        self.foundation = DomainService(self.database, self.clock)
        self.service = EmergencyDutyService(self.database, self.clock, RecordingTransport())
        self._bootstrap()

    def tearDown(self):
        self.database.close()

    def _bootstrap(self):
        f = self.foundation
        f.register_organization(request_id="org", actor_id="bootstrap",
                                organization_id="o1", name="医院")
        f.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                         display_name="管理员", role="admin", organization_id="o1")
        for aid, name in (("op1", "张"), ("op2", "王"), ("op3", "李")):
            f.register_actor(request_id=aid, actor_id="a1", new_actor_id=aid,
                             display_name=name, role="operator", organization_id="o1")
        f.register_site(request_id="site", actor_id="a1", site_id="s1", organization_id="o1",
                        name="急诊", timezone_name="Asia/Shanghai")

    def _qualify(self, actor, position="lead"):
        return self.service.grant_qualification(
            request_id=f"q-{actor}-{position}", actor_id="a1",
            qualification_id=f"qid-{actor}-{position}", site_id="s1", target_actor_id=actor,
            position_code=position, valid_from="2026-09-25T00:00Z",
            valid_until="2026-09-27T00:00Z")

    def _night_shift(self, shift_id="sh1", holders=None):
        holders = holders or [("lead", "op1")]
        return self.service.publish_shift(
            request_id=f"pub-{shift_id}", actor_id="a1", shift_id=shift_id, site_id="s1",
            starts_at="2026-09-25T13:00Z", ends_at="2026-09-26T13:00Z",
            assignments=[{"position_code": p, "holder_actor_id": h} for p, h in holders])

    def test_cross_midnight_shift_uses_site_timezone_date(self):
        # UTC 13:00 = 上海 21:00；UTC 16:00 = 上海次日 00:00，日期仍归开始当天。
        self.assertEqual("2026-09-25",
                         shift_local_date(parse_utc("2026-09-25T13:00Z"),
                                          parse_utc("2026-09-26T13:00Z"), "Asia/Shanghai"))
        self.assertEqual("2026-09-26",
                         shift_local_date(parse_utc("2026-09-26T13:00Z"),
                                          parse_utc("2026-09-27T13:00Z"), "Asia/Shanghai"))

    def test_shift_publish_requires_valid_qualification(self):
        with self.assertRaises(HandoverBlockedError):
            self._night_shift(holders=[("lead", "op2")])
        self._qualify("op1")
        result = self._night_shift(holders=[("lead", "op1")])
        self.assertEqual(1, result["version"])

    def test_shift_versions_supersede_previous(self):
        self._qualify("op1")
        self._night_shift("sh1")
        v2 = self._night_shift("sh2")
        self.assertEqual(2, v2["version"])
        self.assertFalse(self.service.get_shift("sh1").active)
        self.assertTrue(self.service.get_shift("sh2").active)

    def _handover_ready(self):
        self._qualify("op1")
        self._night_shift()
        self.service.register_patient(request_id="p1", actor_id="op1", patient_id="p1",
                                      site_id="s1", display_name="患者", care_state="observation")
        return self.service.propose_handover(request_id="ho1", actor_id="op1", shift_id="sh1",
                                             position_code="lead", incoming_actor_id="op2")

    def test_handover_requires_both_confirmations(self):
        proposal = self._handover_ready()
        self.service.confirm_handover(request_id="c1", actor_id="op1",
                                      handover_id=proposal["handover_id"])
        handover = self.service.get_handover(proposal["handover_id"])
        self.assertEqual("proposed", handover.status)
        self.assertTrue(handover.outgoing_confirmed)
        self.assertFalse(handover.incoming_confirmed)

    def test_handover_blocked_without_qualification_then_effective(self):
        proposal = self._handover_ready()
        self.service.confirm_handover(request_id="c1", actor_id="op1",
                                      handover_id=proposal["handover_id"])
        blocked = self.service.confirm_handover(request_id="c2", actor_id="op2",
                                                handover_id=proposal["handover_id"])
        self.assertEqual("blocked", blocked["status"])
        self.assertEqual("qualification_invalid", blocked["blocked_reason"])
        # 未生效，责任人不变。
        self.assertEqual("op1", self.service.current_responsibility("s1")
                         ["responsible_actors"][0]["actor_id"])
        self._qualify("op2")
        effective = self.service.retry_handover(request_id="r1", actor_id="op2",
                                                handover_id=proposal["handover_id"])
        self.assertEqual("effective", effective["status"])
        self.assertEqual("op2", self.service.current_responsibility("s1")
                         ["responsible_actors"][0]["actor_id"])

    def test_handover_blocked_by_critical_rescue_item(self):
        proposal = self._handover_ready()
        self._qualify("op2")
        item = self.service.add_handover_item(request_id="i1", actor_id="op1", shift_id="sh1",
                                              position_code="lead", kind="rescue",
                                              summary="抢救中", patient_id="p1", critical=True)
        self.service.update_patient_state(request_id="ps1", actor_id="op1", patient_id="p1",
                                          care_state="rescue")
        self.service.confirm_handover(request_id="c1", actor_id="op1",
                                      handover_id=proposal["handover_id"])
        blocked = self.service.confirm_handover(request_id="c2", actor_id="op2",
                                                handover_id=proposal["handover_id"])
        self.assertEqual("critical_rescue_items_open", blocked["blocked_reason"])
        # 抢救结束后生效；事项仍挂在岗位上，没有被自动转移。
        self.service.update_patient_state(request_id="ps2", actor_id="op1", patient_id="p1",
                                          care_state="observation")
        effective = self.service.retry_handover(request_id="r2", actor_id="op2",
                                                handover_id=proposal["handover_id"])
        self.assertEqual("effective", effective["status"])
        items = self.service.list_handover_items("sh1")
        self.assertEqual("open", items[0].status)

    def test_rescue_item_cannot_be_transferred(self):
        self._qualify("op1")
        self._qualify("op3", "support")
        self._night_shift(holders=[("lead", "op1"), ("support", "op3")])
        self.service.register_patient(request_id="p1", actor_id="op1", patient_id="p1",
                                      site_id="s1", display_name="患者", care_state="rescue")
        item = self.service.add_handover_item(request_id="i1", actor_id="op1", shift_id="sh1",
                                              position_code="lead", kind="rescue",
                                              summary="抢救", patient_id="p1", critical=True)
        with self.assertRaises(HandoverBlockedError):
            self.service.transfer_handover_item(request_id="t1", actor_id="op1",
                                                item_id=item["item_id"], to_shift_id="sh1",
                                                to_position_code="support")
        self.service.update_patient_state(request_id="ps1", actor_id="op1", patient_id="p1",
                                          care_state="observation")
        moved = self.service.transfer_handover_item(request_id="t2", actor_id="op1",
                                                    item_id=item["item_id"], to_shift_id="sh1",
                                                    to_position_code="support")
        self.assertEqual("transferred", moved["status"])

    def test_revoke_authorization_blocks_disclosure_but_keeps_history(self):
        self._qualify("op1")
        self._night_shift()
        self.service.register_patient(request_id="p1", actor_id="op1", patient_id="p1",
                                      site_id="s1", display_name="患者", care_state="observation")
        grant = self.service.grant_authorization(request_id="g1", actor_id="op1", patient_id="p1",
                                                 contact_name="家属", contact_channel="phone-1",
                                                 scope=["condition"])
        self.service.log_disclosure(request_id="d1", actor_id="op1",
                                    authorization_id=grant["authorization_id"],
                                    summary="首次告知")
        self.service.revoke_authorization(request_id="rv1", actor_id="op1",
                                          authorization_id=grant["authorization_id"])
        with self.assertRaises(PermissionDenied):
            self.service.log_disclosure(request_id="d2", actor_id="op1",
                                        authorization_id=grant["authorization_id"],
                                        summary="撤回后告知")
        history = self.service.list_access_history(grant["authorization_id"])
        self.assertEqual(1, len(history))
        self.assertEqual([], self.service.disclosure_scope("p1"))

    def test_notification_safe_replay_versus_content_conflict(self):
        self._qualify("op1")
        self._night_shift()
        self.service.register_patient(request_id="p1", actor_id="op1", patient_id="p1",
                                      site_id="s1", display_name="患者", care_state="observation")
        grant = self.service.grant_authorization(request_id="g1", actor_id="op1", patient_id="p1",
                                                 contact_name="家属", contact_channel="phone-1",
                                                 scope=["condition"])
        kwargs = dict(actor_id="op1", site_id="s1", patient_id="p1",
                      authorization_id=grant["authorization_id"], channel="sms",
                      subject="简报", content="稳定")
        first = self.service.submit_notification(request_id="n1", **kwargs)
        replay = self.service.submit_notification(request_id="n1", **kwargs)
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        with self.assertRaises(DeliveryConflictError):
            self.service.submit_notification(request_id="n1", actor_id="op1", site_id="s1",
                                             patient_id="p1",
                                             authorization_id=grant["authorization_id"],
                                             channel="sms", subject="简报", content="内容变了")

    def test_window_blocks_until_next_open(self):
        # 当前上海时间 22:00；窗口 08:00-21:00。
        moment = self.clock.now()
        self.assertFalse(within_window(moment.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Shanghai")),
                                       "08:00", "21:00"))
        nxt = next_window_open(moment, "Asia/Shanghai", "08:00", "21:00")
        self.assertEqual(parse_utc("2026-09-26T00:00Z"), nxt)

    def test_failed_delivery_enters_bounded_manual_handling(self):
        failing = EmergencyDutyService(self.database, self.clock, FailingTransport())
        self._qualify("op1")
        self._night_shift()
        self.service.register_patient(request_id="p1", actor_id="op1", patient_id="p1",
                                      site_id="s1", display_name="患者", care_state="observation")
        grant = self.service.grant_authorization(request_id="g1", actor_id="op1", patient_id="p1",
                                                 contact_name="家属", contact_channel="phone-1",
                                                 scope=["condition"])
        failing.submit_notification(request_id="n1", actor_id="op1", site_id="s1", patient_id="p1",
                                    authorization_id=grant["authorization_id"], channel="sms",
                                    subject="简报", content="稳定")
        self.assertEqual(1, failing.run_maintenance()["retried"])
        self.clock.set(datetime(2026, 9, 25, 14, 2, tzinfo=timezone.utc))
        self.assertEqual(1, failing.run_maintenance()["retried"])
        self.clock.set(datetime(2026, 9, 25, 14, 8, tzinfo=timezone.utc))
        result = failing.run_maintenance()
        self.assertEqual(1, result["manual"])
        self.assertEqual(0, result["retried"])
        notification = failing.get_notification("n1")
        self.assertEqual("failed", notification.status)
        self.assertEqual(3, notification.attempt_count)
        tasks = failing.list_manual_tasks("s1", status="open")
        self.assertEqual(1, len(tasks))
        # 超过人工处置期限后自动关闭，不再等待。
        self.clock.set(datetime(2026, 9, 25, 17, 0, tzinfo=timezone.utc))
        self.assertEqual(1, self.service.expire_manual_tasks())
        self.assertEqual("expired",
                         self.service.list_manual_tasks("s1", status="expired")[0].status)

    def test_revoke_blocks_pending_notification_immediately(self):
        self._qualify("op1")
        self._night_shift()
        self.service.register_patient(request_id="p1", actor_id="op1", patient_id="p1",
                                      site_id="s1", display_name="患者", care_state="observation")
        grant = self.service.grant_authorization(request_id="g1", actor_id="op1", patient_id="p1",
                                                 contact_name="家属", contact_channel="phone-1",
                                                 scope=["condition"],
                                                 window_start="08:00", window_end="21:00")
        self.service.submit_notification(request_id="n1", actor_id="op1", site_id="s1",
                                         patient_id="p1",
                                         authorization_id=grant["authorization_id"], channel="sms",
                                         subject="简报", content="等待窗口")
        self.assertEqual(1, self.service.run_maintenance()["waiting_window"])
        self.service.revoke_authorization(request_id="rv1", actor_id="op1",
                                          authorization_id=grant["authorization_id"])
        # 撤回在同一事务内立即阻断，调度器再跑也不会重复处理或转入人工。
        self.assertEqual("blocked", self.service.get_notification("n1").status)
        outcome = self.service.run_maintenance()
        self.assertEqual(0, outcome["blocked"])
        self.assertEqual([], self.service.list_manual_tasks("s1"))

    def test_state_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ed.sqlite3"
            database = Database(path)
            service = EmergencyDutyService(database, self.clock, RecordingTransport())
            DomainService(database, self.clock).register_organization(
                request_id="org", actor_id="bootstrap", organization_id="o1", name="医院")
            DomainService(database, self.clock).register_actor(
                request_id="admin", actor_id="bootstrap", new_actor_id="a1", display_name="管理员",
                role="admin", organization_id="o1")
            DomainService(database, self.clock).register_actor(
                request_id="op1", actor_id="a1", new_actor_id="op1", display_name="张",
                role="operator", organization_id="o1")
            DomainService(database, self.clock).register_site(
                request_id="site", actor_id="a1", site_id="s1", organization_id="o1", name="急诊",
                timezone_name="Asia/Shanghai")
            service.grant_qualification(request_id="q1", actor_id="a1", qualification_id="qid1",
                                        site_id="s1", target_actor_id="op1", position_code="lead",
                                        valid_from="2026-09-25T00:00Z",
                                        valid_until="2026-09-27T00:00Z")
            service.publish_shift(request_id="sh1", actor_id="a1", shift_id="sh1", site_id="s1",
                                  starts_at="2026-09-25T13:00Z",
                                  ends_at="2026-09-26T13:00Z",
                                  assignments=[{"position_code": "lead",
                                                "holder_actor_id": "op1"}])
            database.close()

            database = Database(path)
            reopened = EmergencyDutyService(database, self.clock, RecordingTransport())
            responsibility = reopened.current_responsibility("s1")
            self.assertEqual("sh1", responsibility["current_shift"]["shift_id"])
            self.assertEqual("op1", responsibility["responsible_actors"][0]["actor_id"])
            valid, _ = DomainService(database).verify_audit()
            self.assertTrue(valid)
            database.close()

    def test_auditor_cannot_change_duty_state(self):
        self.foundation.register_actor(request_id="au", actor_id="a1", new_actor_id="au1",
                                       display_name="审计", role="auditor", organization_id="o1")
        with self.assertRaises(PermissionDenied):
            self.service.register_patient(request_id="px", actor_id="au1", patient_id="px",
                                          site_id="s1", display_name="患者")


if __name__ == "__main__":
    unittest.main()
