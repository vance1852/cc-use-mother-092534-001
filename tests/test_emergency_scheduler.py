import time
import unittest
from datetime import datetime, timezone

from festival_foundation.service import DomainService
from festival_foundation.storage import Database

from emergency_duty.clock_support import SteppingClock
from emergency_duty.scheduler import MaintenanceWorker
from emergency_duty.service import EmergencyDutyService
from emergency_duty.transports import FlakyTransport


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = SteppingClock(datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc))
        foundation = DomainService(self.database, self.clock)
        foundation.register_organization(request_id="org", actor_id="bootstrap",
                                         organization_id="o1", name="医院")
        foundation.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                  display_name="管理员", role="admin", organization_id="o1")
        foundation.register_actor(request_id="op1", actor_id="a1", new_actor_id="op1",
                                  display_name="张", role="operator", organization_id="o1")
        foundation.register_site(request_id="site", actor_id="a1", site_id="s1",
                                 organization_id="o1", name="急诊", timezone_name="Asia/Shanghai")
        self.service = EmergencyDutyService(self.database, self.clock, FlakyTransport(1))
        self.service.grant_qualification(request_id="q1", actor_id="a1", qualification_id="qid1",
                                         site_id="s1", target_actor_id="op1", position_code="lead",
                                         valid_from="2026-09-25T00:00Z",
                                         valid_until="2026-09-27T00:00Z")
        self.service.publish_shift(request_id="sh1", actor_id="a1", shift_id="sh1", site_id="s1",
                                   starts_at="2026-09-25T13:00Z",
                                   ends_at="2026-09-26T13:00Z",
                                   assignments=[{"position_code": "lead",
                                                 "holder_actor_id": "op1"}])
        self.service.register_patient(request_id="p1", actor_id="op1", patient_id="p1",
                                      site_id="s1", display_name="患者", care_state="observation")
        grant = self.service.grant_authorization(request_id="g1", actor_id="op1", patient_id="p1",
                                                 contact_name="家属", contact_channel="phone-1",
                                                 scope=["condition"])
        self.auth_id = grant["authorization_id"]

    def tearDown(self):
        self.database.close()

    def test_worker_processes_due_items_in_background(self):
        self.service.submit_notification(request_id="n1", actor_id="op1", site_id="s1",
                                         patient_id="p1", authorization_id=self.auth_id,
                                         channel="sms", subject="简报", content="稳定")
        ticks = []
        worker = MaintenanceWorker(self.service, interval_seconds=0.02, sink=ticks.append)
        worker.start()
        # 首次故障：立即维护产生一次重试。
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not ticks:
            time.sleep(0.01)
        self.clock.set(datetime(2026, 9, 25, 14, 2, tzinfo=timezone.utc))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and \
                self.service.get_notification("n1").status != "delivered":
            time.sleep(0.01)
        worker.stop(timeout=2)
        self.assertEqual("delivered", self.service.get_notification("n1").status)


if __name__ == "__main__":
    unittest.main()
