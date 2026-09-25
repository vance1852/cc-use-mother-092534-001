import unittest
from datetime import datetime, timezone

from festival_foundation.service import DomainService
from festival_foundation.storage import Database

from emergency_duty.api import combined_route
from emergency_duty.clock_support import SteppingClock
from emergency_duty.service import EmergencyDutyService
from emergency_duty.transports import RecordingTransport


class EmergencyApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = SteppingClock(datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc))
        self.foundation = DomainService(self.database, self.clock)
        self.service = EmergencyDutyService(self.database, self.clock, RecordingTransport())
        self.foundation.register_organization(request_id="org", actor_id="bootstrap",
                                              organization_id="o1", name="医院")
        self.foundation.register_actor(request_id="admin", actor_id="bootstrap",
                                      new_actor_id="a1", display_name="管理员", role="admin",
                                      organization_id="o1")
        self.foundation.register_actor(request_id="op1", actor_id="a1", new_actor_id="op1",
                                       display_name="张", role="operator", organization_id="o1")
        self.foundation.register_site(request_id="site", actor_id="a1", site_id="s1",
                                      organization_id="o1", name="急诊",
                                      timezone_name="Asia/Shanghai")
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

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor="a1"):
        return combined_route(self.foundation, self.service, method, path, body,
                              {"X-Actor-Id": actor})

    def test_foundation_routes_still_served(self):
        status, payload = self.call("GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_responsibility_and_duty_snapshot(self):
        status, payload = self.call("GET", "/ed/responsibility?site_id=s1", None, actor="op1")
        self.assertEqual(200, status)
        self.assertEqual("sh1", payload["current_shift"]["shift_id"])
        self.assertEqual("op1", payload["responsible_actors"][0]["actor_id"])
        status, payload = self.call("GET", "/ed/duty?site_id=s1", None)
        self.assertEqual(200, status)
        self.assertEqual("s1", payload["site_id"])
        self.assertIn("disclosure_scopes", payload)

    def test_full_handover_chain_over_api(self):
        self.foundation.register_actor(request_id="op2", actor_id="a1", new_actor_id="op2",
                                       display_name="王", role="operator", organization_id="o1")
        status, payload = self.call("POST", "/ed/handovers", {
            "request_id": "ho1", "shift_id": "sh1",
            "position_code": "lead", "incoming_actor_id": "op2", "reason": "支援"}, actor="op1")
        self.assertEqual(201, status)
        handover_id = payload["handover_id"]
        status, payload = self.call("POST", "/ed/handovers/confirm",
                                    {"request_id": "c1", "handover_id": handover_id}, actor="op1")
        self.assertEqual(201, status)
        # 接任者无资格：确认已记录，但生效被阻止（决定可追溯）。
        status, payload = self.call("POST", "/ed/handovers/confirm",
                                    {"request_id": "c2", "handover_id": handover_id}, actor="op2")
        self.assertEqual(201, status)
        self.assertEqual("blocked", payload["status"])
        self.assertEqual("qualification_invalid", payload["blocked_reason"])
        # 每次交班决定可查。
        status, payload = self.call("GET", f"/ed/handovers/{handover_id}", None)
        self.assertEqual(200, status)
        self.assertEqual("proposed", payload["status"])
        self.assertEqual("qualification_invalid", payload["blocked_reason"])

    def test_notification_conflict_returns_distinct_error(self):
        grant = self.call("POST", "/ed/authorizations", {
            "request_id": "g1", "patient_id": "p1", "contact_name": "家属",
            "contact_channel": "phone-1", "scope": ["condition"]}, actor="op1")[1]
        body = {"request_id": "n1", "site_id": "s1", "patient_id": "p1",
                "authorization_id": grant["authorization_id"], "channel": "sms",
                "subject": "简报", "content": "稳定"}
        self.assertEqual(201, self.call("POST", "/ed/notifications", body, actor="op1")[0])
        self.assertEqual(200, self.call("POST", "/ed/notifications", body, actor="op1")[0])
        changed = {**body, "content": "内容变化"}
        status, payload = self.call("POST", "/ed/notifications", changed, actor="op1")
        self.assertEqual(409, status)
        self.assertEqual("delivery_conflict", payload["error"])

    def test_manual_tasks_listed_under_pending_contacts(self):
        grant = self.call("POST", "/ed/authorizations", {
            "request_id": "g1", "patient_id": "p1", "contact_name": "家属",
            "contact_channel": "phone-1", "scope": ["condition"]}, actor="op1")[1]
        self.call("POST", "/ed/notifications", {
            "request_id": "n1", "site_id": "s1", "patient_id": "p1",
            "authorization_id": grant["authorization_id"], "channel": "sms",
            "subject": "简报", "content": "稳定"}, actor="op1")
        status, payload = self.call("GET", "/ed/pending-contacts?site_id=s1", None)
        self.assertEqual(200, status)
        self.assertEqual("n1", payload["pending_requests"][0]["request_id"])

    def test_requires_actor_header(self):
        status, payload = combined_route(self.foundation, self.service, "POST",
                                         "/ed/patients",
                                         {"request_id": "px", "patient_id": "px", "site_id": "s1",
                                          "display_name": "x"}, {})
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
