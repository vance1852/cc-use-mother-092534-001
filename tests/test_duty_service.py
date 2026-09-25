import unittest
from datetime import datetime, timedelta, timezone

from festival_duty.gateway import DeliveryFailure
from festival_duty.service import DutyService
from festival_foundation.clock import FixedClock
from festival_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from festival_foundation.service import DomainService
from festival_foundation.storage import Database


T0 = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, value):
        self._value = value

    def now(self):
        return self._value

    def set(self, value):
        self._value = value


class FailingGateway:
    def __init__(self, times=None):
        self.times = times
        self.calls = 0

    def deliver(self, *, notification, authorization, summary):
        self.calls += 1
        if self.times is None or self.calls <= self.times:
            raise DeliveryFailure("渠道故障")
        return "recovered-channel"


class DutyServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(T0)
        self.foundation = DomainService(self.database, self.clock)
        self.foundation.register_organization(request_id="org", actor_id="bootstrap",
                                              organization_id="o1", name="急诊值守中心")
        self.foundation.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                       display_name="管理员", role="admin", organization_id="o1")
        self.foundation.register_actor(request_id="op1", actor_id="a1", new_actor_id="op1",
                                       display_name="医生甲", role="operator", organization_id="o1")
        self.foundation.register_actor(request_id="op2", actor_id="a1", new_actor_id="op2",
                                       display_name="医生乙", role="operator", organization_id="o1")
        self.foundation.register_actor(request_id="rev1", actor_id="a1", new_actor_id="rev1",
                                       display_name="护士长", role="reviewer", organization_id="o1")
        self.foundation.register_actor(request_id="au1", actor_id="a1", new_actor_id="au1",
                                       display_name="审计员", role="auditor", organization_id="o1")
        self.foundation.register_site(request_id="site", actor_id="a1", site_id="s1",
                                      organization_id="o1", name="急诊一区", timezone_name="Asia/Shanghai")
        self.duty = DutyService(self.foundation)
        self.seq = 0

    def tearDown(self):
        self.database.close()

    def rid(self):
        self.seq += 1
        return f"req-{self.seq}"

    def qual(self, target, position="rescue", expires="2026-10-25T00:00:00Z"):
        return self.duty.grant_qualification(request_id=self.rid(), actor_id="a1",
                                             target_actor_id=target, position=position,
                                             expires_at=expires)

    def shift(self, holder="op1", shift_id="shift-1", position="rescue",
              starts="2026-09-25T12:00:00Z", ends="2026-09-26T00:00:00Z"):
        return self.duty.create_shift(request_id=self.rid(), actor_id="a1", site_id="s1",
                                      position=position, holder_actor_id=holder,
                                      starts_at=starts, ends_at=ends, shift_id=shift_id)

    def authorize(self, patient="p1", contact="家属甲", scope="condition_summary", auth_id=None):
        return self.duty.grant_authorization(request_id=self.rid(), actor_id="op1", site_id="s1",
                                             patient_ref=patient, contact_name=contact,
                                             contact_channel="phone", scope=scope,
                                             authorization_id=auth_id)

    def notify(self, auth_id="auth-x", notification_id=None, scope="condition_summary",
               summary="病情摘要", window_start="2026-09-25T08:00:00Z",
               window_end="2026-09-25T16:00:00Z", max_attempts=None, request_id=None):
        kwargs = {}
        if max_attempts is not None:
            kwargs["max_attempts"] = max_attempts
        return self.duty.request_notification(request_id=request_id or self.rid(), actor_id="op1",
                                              site_id="s1", patient_ref="p1",
                                              authorization_id=auth_id, scope=scope, summary=summary,
                                              window_start=window_start, window_end=window_end,
                                              notification_id=notification_id, **kwargs)

    # ------------------------------------------------------------------
    # 班次与岗位资格
    # ------------------------------------------------------------------

    def test_shift_requires_valid_qualification(self):
        with self.assertRaises(PermissionDenied):
            self.shift()
        self.qual("op1")
        receipt = self.shift()
        self.assertFalse(receipt.replayed)

    def test_cross_midnight_shift_belongs_to_site_timezone_date(self):
        self.qual("op1")
        # UTC 16:30 对应院区时区次日 00:30，服务日归属次日。
        self.shift(shift_id="shift-late", starts="2026-09-25T16:30:00Z", ends="2026-09-26T04:00:00Z")
        self.assertEqual([], self.duty.list_shifts("s1", service_date="2026-09-25"))
        shifts = self.duty.list_shifts("s1", service_date="2026-09-26")
        self.assertEqual(1, len(shifts))
        self.assertEqual("2026-09-26", shifts[0].service_date)
        self.assertEqual("Asia/Shanghai", shifts[0].timezone_name)

    def test_shift_rejects_unknown_position_and_bad_window(self):
        self.qual("op1")
        with self.assertRaises(ValidationError):
            self.shift(position="triage")
        with self.assertRaises(ValidationError):
            self.shift(starts="2026-09-26T00:00:00Z", ends="2026-09-25T12:00:00Z")

    def test_auditor_cannot_manage_shifts(self):
        self.qual("op1")
        with self.assertRaises(PermissionDenied):
            self.duty.create_shift(request_id=self.rid(), actor_id="au1", site_id="s1",
                                   position="rescue", holder_actor_id="op1",
                                   starts_at="2026-09-25T12:00:00Z", ends_at="2026-09-26T00:00:00Z")

    # ------------------------------------------------------------------
    # 临时换岗
    # ------------------------------------------------------------------

    def test_swap_takes_effect_only_after_both_confirm(self):
        self.qual("op1")
        self.qual("op2")
        self.shift()
        self.duty.request_swap(request_id=self.rid(), actor_id="op1", shift_id="shift-1",
                               to_actor_id="op2", reason="临时支援", swap_id="swap-1")
        state = self.duty.confirm_swap(actor_id="op1", swap_id="swap-1")
        self.assertEqual("pending", state["status"])
        self.assertEqual("op1", self.duty.current_responsible(site_id="s1", position="rescue")["holder_actor_id"])
        state = self.duty.confirm_swap(actor_id="op2", swap_id="swap-1")
        self.assertEqual("applied", state["status"])
        responsible = self.duty.current_responsible(site_id="s1", position="rescue")
        self.assertEqual("op2", responsible["holder_actor_id"])
        self.assertEqual(2, responsible["version"])

    def test_swap_requires_successor_qualification_at_request_and_effect(self):
        self.qual("op1")
        self.shift()
        with self.assertRaises(PermissionDenied):
            self.duty.request_swap(request_id=self.rid(), actor_id="op1", shift_id="shift-1",
                                   to_actor_id="op2")
        # 资格在确认间隙过期，则换岗不生效；补齐资格后可再次确认生效。
        self.duty.grant_qualification(request_id=self.rid(), actor_id="a1", target_actor_id="op2",
                                      position="rescue", expires_at="2026-09-25T13:00:00Z",
                                      qualification_id="qual-short")
        self.duty.request_swap(request_id=self.rid(), actor_id="op1", shift_id="shift-1",
                               to_actor_id="op2", swap_id="swap-1")
        self.clock.set(T0 + timedelta(hours=2))
        self.duty.confirm_swap(actor_id="op1", swap_id="swap-1")
        with self.assertRaises(PermissionDenied):
            self.duty.confirm_swap(actor_id="op2", swap_id="swap-1")
        self.assertEqual("op1", self.duty.current_responsible(site_id="s1", position="rescue")["holder_actor_id"])
        self.duty.grant_qualification(request_id=self.rid(), actor_id="a1", target_actor_id="op2",
                                      position="rescue", expires_at="2026-09-26T00:00:00Z",
                                      qualification_id="qual-long")
        state = self.duty.confirm_swap(actor_id="op2", swap_id="swap-1")
        self.assertEqual("applied", state["status"])

    def test_swap_confirm_only_by_parties(self):
        self.qual("op1")
        self.qual("op2")
        self.shift()
        self.duty.request_swap(request_id=self.rid(), actor_id="op1", shift_id="shift-1",
                               to_actor_id="op2", swap_id="swap-1")
        with self.assertRaises(PermissionDenied):
            self.duty.confirm_swap(actor_id="rev1", swap_id="swap-1")

    def test_swap_decline_and_duplicate_pending(self):
        self.qual("op1")
        self.qual("op2")
        self.shift()
        self.duty.request_swap(request_id=self.rid(), actor_id="op1", shift_id="shift-1",
                               to_actor_id="op2", swap_id="swap-1")
        with self.assertRaises(ConflictError):
            self.duty.request_swap(request_id=self.rid(), actor_id="op1", shift_id="shift-1",
                                   to_actor_id="op2")
        state = self.duty.decline_swap(actor_id="op2", swap_id="swap-1")
        self.assertEqual("declined", state["status"])
        with self.assertRaises(ConflictError):
            self.duty.confirm_swap(actor_id="op1", swap_id="swap-1")

    def test_swap_to_current_holder_rejected(self):
        self.qual("op1")
        self.shift()
        with self.assertRaises(ValidationError):
            self.duty.request_swap(request_id=self.rid(), actor_id="op1", shift_id="shift-1",
                                   to_actor_id="op1")

    # ------------------------------------------------------------------
    # 授权与披露
    # ------------------------------------------------------------------

    def test_revoke_blocks_new_request_and_pending_delivery_but_keeps_history(self):
        self.authorize(auth_id="auth-1")
        self.notify(auth_id="auth-1", notification_id="n1")
        self.notify(auth_id="auth-1", notification_id="n2")
        delivered = self.duty.deliver_notification(actor_id="op1", notification_id="n1")
        self.assertEqual("delivered", delivered["status"])
        self.duty.revoke_authorization(request_id=self.rid(), actor_id="a1", authorization_id="auth-1")
        with self.assertRaises(PermissionDenied):
            self.notify(auth_id="auth-1")
        with self.assertRaises(PermissionDenied):
            self.duty.deliver_notification(actor_id="op1", notification_id="n2")
        scope = self.duty.disclosable_scope(site_id="s1", patient_ref="p1", contact_name="家属甲")
        self.assertEqual("revoked", scope["status"])
        self.assertEqual("none", scope["scope"])
        disclosures = self.duty.list_disclosures("s1")
        self.assertEqual(1, len(disclosures))
        self.assertEqual("auth-1", disclosures[0].authorization_id)

    def test_disclosable_scope_without_authorization(self):
        scope = self.duty.disclosable_scope(site_id="s1", patient_ref="p9", contact_name="陌生人")
        self.assertEqual("none", scope["status"])
        self.assertEqual("none", scope["scope"])

    def test_notification_scope_cannot_exceed_authorization(self):
        self.authorize(auth_id="auth-1", scope="condition_summary")
        with self.assertRaises(PermissionDenied):
            self.notify(auth_id="auth-1", scope="critical_updates")
        receipt = self.notify(auth_id="auth-1", scope="identity_only")
        self.assertFalse(receipt.replayed)

    def test_notification_replay_and_conflict(self):
        self.authorize(auth_id="auth-1")
        first = self.notify(auth_id="auth-1", request_id="notif-req", summary="首次内容")
        replay = self.notify(auth_id="auth-1", request_id="notif-req", summary="首次内容")
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        with self.assertRaises(ConflictError):
            self.notify(auth_id="auth-1", request_id="notif-req", summary="改动后的内容")

    # ------------------------------------------------------------------
    # 投递、通知窗口与人工处置
    # ------------------------------------------------------------------

    def test_failed_delivery_enters_manual_handling_with_deadline(self):
        self.authorize(auth_id="auth-1")
        failing = DutyService(self.foundation, gateway=FailingGateway(), manual_ttl_seconds=3600)
        self.notify(auth_id="auth-1", notification_id="n1", max_attempts=2)
        first = failing.deliver_notification(actor_id="op1", notification_id="n1")
        self.assertEqual("pending", first["status"])
        self.assertEqual(1, first["attempts"])
        second = failing.deliver_notification(actor_id="op1", notification_id="n1")
        self.assertEqual("manual_handling", second["status"])
        self.assertEqual("2026-09-25T13:00:00Z", second["manual_due_at"])
        with self.assertRaises(ConflictError):
            failing.deliver_notification(actor_id="op1", notification_id="n1")
        resolved = self.duty.resolve_manual_notification(actor_id="rev1", notification_id="n1",
                                                         outcome="delivered", note="电话已接通")
        self.assertEqual("delivered", resolved["status"])
        self.assertEqual(1, len(self.duty.list_disclosures("s1")))

    def test_manual_handling_expires_after_deadline(self):
        self.authorize(auth_id="auth-1")
        failing = DutyService(self.foundation, gateway=FailingGateway(), manual_ttl_seconds=3600)
        self.notify(auth_id="auth-1", notification_id="n1", max_attempts=1)
        failing.deliver_notification(actor_id="op1", notification_id="n1")
        self.clock.set(T0 + timedelta(hours=2))
        result = self.duty.process_pending()
        self.assertEqual(1, result["expired"])
        self.assertEqual("expired", self.duty.get_notification("n1").status)
        with self.assertRaises(ConflictError):
            self.duty.resolve_manual_notification(actor_id="rev1", notification_id="n1",
                                                  outcome="delivered")

    def test_window_missed_moves_to_manual_on_sweep(self):
        self.authorize(auth_id="auth-1")
        self.notify(auth_id="auth-1", notification_id="n1",
                    window_start="2026-09-25T09:00:00Z", window_end="2026-09-25T10:00:00Z")
        result = self.duty.process_pending()
        self.assertEqual({"moved_to_manual": 1, "expired": 0}, result)
        notification = self.duty.get_notification("n1")
        self.assertEqual("manual_handling", notification.status)
        self.assertIsNotNone(notification.manual_due_at)

    def test_deliver_outside_window_rejected(self):
        self.authorize(auth_id="auth-1")
        self.notify(auth_id="auth-1", notification_id="n1",
                    window_start="2026-09-25T13:00:00Z", window_end="2026-09-25T16:00:00Z")
        with self.assertRaises(ValidationError):
            self.duty.deliver_notification(actor_id="op1", notification_id="n1")

    def test_deliver_after_window_moves_to_manual(self):
        self.authorize(auth_id="auth-1")
        self.notify(auth_id="auth-1", notification_id="n1",
                    window_start="2026-09-25T08:00:00Z", window_end="2026-09-25T11:00:00Z")
        result = self.duty.deliver_notification(actor_id="op1", notification_id="n1")
        self.assertEqual("manual_handling", result["status"])
        self.assertEqual("window_missed", result["reason"])

    def test_flaky_gateway_recovers_before_attempt_limit(self):
        self.authorize(auth_id="auth-1")
        flaky = DutyService(self.foundation, gateway=FailingGateway(times=1))
        self.notify(auth_id="auth-1", notification_id="n1", max_attempts=3)
        self.assertEqual("pending", flaky.deliver_notification(actor_id="op1", notification_id="n1")["status"])
        result = flaky.deliver_notification(actor_id="op1", notification_id="n1")
        self.assertEqual("delivered", result["status"])
        self.assertEqual(2, result["attempts"])

    # ------------------------------------------------------------------
    # 交班
    # ------------------------------------------------------------------

    def _two_shifts_with_items(self):
        self.qual("op1")
        self.qual("op2")
        self.shift(shift_id="shift-1")
        self.shift(holder="op2", shift_id="shift-2",
                   starts="2026-09-26T00:00:00Z", ends="2026-09-26T12:00:00Z")
        self.duty.create_handover_item(request_id=self.rid(), actor_id="op1", site_id="s1",
                                       shift_id="shift-1", patient_ref="p1",
                                       category="observation", summary="留观复查", item_id="item-open")
        self.duty.create_handover_item(request_id=self.rid(), actor_id="op1", site_id="s1",
                                       shift_id="shift-1", patient_ref="p2",
                                       category="rescue", summary="抢救中", in_rescue=True,
                                       item_id="item-rescue")

    def test_handover_transfers_open_and_retains_rescue_items(self):
        self._two_shifts_with_items()
        result = self.duty.perform_handover(request_id="handover-1", actor_id="op1",
                                            from_shift_id="shift-1", to_shift_id="shift-2")
        self.assertFalse(result["replayed"])
        decisions = {d["item_id"]: d["decision"] for d in result["decisions"]}
        self.assertEqual("transferred", decisions["item-open"])
        self.assertEqual("retained_rescue", decisions["item-rescue"])
        moved = self.duty.list_handover_items("s1", shift_id="shift-2")
        self.assertEqual(["item-open"], [item.item_id for item in moved])
        kept = self.duty.list_handover_items("s1", shift_id="shift-1")
        self.assertEqual(["item-rescue"], [item.item_id for item in kept])

    def test_handover_replay_does_not_duplicate_decisions(self):
        self._two_shifts_with_items()
        first = self.duty.perform_handover(request_id="handover-1", actor_id="op1",
                                           from_shift_id="shift-1", to_shift_id="shift-2")
        replay = self.duty.perform_handover(request_id="handover-1", actor_id="op1",
                                            from_shift_id="shift-1", to_shift_id="shift-2")
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(first["decisions"]), len(replay["decisions"]))
        self.assertEqual(2, len(self.duty.list_handover_decisions("s1", batch_id="handover-1")))

    def test_rescue_item_manual_transfer_requires_reason(self):
        self._two_shifts_with_items()
        with self.assertRaises(ValidationError):
            self.duty.transfer_item(request_id=self.rid(), actor_id="op1", item_id="item-rescue",
                                    to_shift_id="shift-2")
        receipt = self.duty.transfer_item(request_id=self.rid(), actor_id="op1", item_id="item-rescue",
                                          to_shift_id="shift-2", reason="家属到院，转运至接班团队")
        self.assertFalse(receipt.replayed)
        decisions = self.duty.list_handover_decisions("s1")
        self.assertEqual("manual_transfer_rescue", decisions[0].decision)

    def test_handover_requires_same_site_and_position(self):
        self.qual("op1")
        self.qual("op2", position="observation")
        self.shift(shift_id="shift-1")
        self.shift(holder="op2", shift_id="shift-2", position="observation",
                   starts="2026-09-26T00:00:00Z", ends="2026-09-26T12:00:00Z")
        with self.assertRaises(ValidationError):
            self.duty.perform_handover(request_id=self.rid(), actor_id="op1",
                                       from_shift_id="shift-1", to_shift_id="shift-2")

    # ------------------------------------------------------------------
    # 重启续办
    # ------------------------------------------------------------------

    def test_restart_continues_unfinished_notifications(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duty.sqlite3"
            database = Database(path)
            foundation = DomainService(database, FixedClock(T0))
            foundation.register_organization(request_id="org", actor_id="bootstrap",
                                             organization_id="o1", name="急诊值守中心")
            foundation.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                      display_name="管理员", role="admin", organization_id="o1")
            foundation.register_actor(request_id="op1", actor_id="a1", new_actor_id="op1",
                                      display_name="医生甲", role="operator", organization_id="o1")
            foundation.register_site(request_id="site", actor_id="a1", site_id="s1",
                                     organization_id="o1", name="急诊一区", timezone_name="Asia/Shanghai")
            duty = DutyService(foundation)
            duty.grant_authorization(request_id="auth", actor_id="op1", site_id="s1",
                                     patient_ref="p1", contact_name="家属甲",
                                     contact_channel="phone", scope="full", authorization_id="auth-1")
            duty.request_notification(request_id="notif", actor_id="op1", site_id="s1", patient_ref="p1",
                                      authorization_id="auth-1", scope="identity_only",
                                      summary="窗口已过的通知",
                                      window_start="2026-09-25T09:00:00Z",
                                      window_end="2026-09-25T10:00:00Z",
                                      notification_id="n1")
            database.close()

            database2 = Database(path)
            foundation2 = DomainService(database2, FixedClock(T0))
            duty2 = DutyService(foundation2)
            recovered = duty2.recover()
            self.assertEqual({"moved_to_manual": 1, "expired": 0}, recovered)
            pending = duty2.list_pending_notifications("s1")
            self.assertEqual(1, len(pending))
            self.assertEqual("manual_handling", pending[0].status)
            valid, _ = foundation2.verify_audit()
            self.assertTrue(valid)
            database2.close()

    def test_current_responsible_not_found(self):
        with self.assertRaises(NotFoundError):
            self.duty.current_responsible(site_id="s1", position="rescue")


if __name__ == "__main__":
    unittest.main()
