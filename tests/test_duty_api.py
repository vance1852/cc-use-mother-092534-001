import unittest
from datetime import datetime, timezone

from festival_duty.api import create_router
from festival_duty.service import DutyService
from festival_foundation.clock import FixedClock
from festival_foundation.service import DomainService
from festival_foundation.storage import Database


class DutyApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.foundation = DomainService(self.database, FixedClock(datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)))
        self.duty = DutyService(self.foundation)
        self.router = create_router(self.foundation, self.duty)
        self.foundation.register_organization(request_id="org", actor_id="bootstrap",
                                              organization_id="o1", name="急诊值守中心")
        self.foundation.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                       display_name="管理员", role="admin", organization_id="o1")
        self.foundation.register_actor(request_id="op1", actor_id="a1", new_actor_id="op1",
                                       display_name="医生甲", role="operator", organization_id="o1")
        self.foundation.register_site(request_id="site", actor_id="a1", site_id="s1",
                                      organization_id="o1", name="急诊一区", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def post(self, path, body, actor="a1"):
        return self.router("POST", path, body, {"X-Actor-Id": actor})

    def get(self, path):
        return self.router("GET", path, None, {})

    def test_foundation_routes_still_work(self):
        status, payload = self.get("/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_duty_route_returns_404(self):
        status, payload = self.get("/duty/nope")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_missing_field_returns_400(self):
        status, payload = self.post("/duty/shifts", {"request_id": "x"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_shift_and_current_responsible_flow(self):
        status, _ = self.post("/duty/qualifications", {
            "request_id": "q1", "target_actor_id": "op1", "position": "rescue",
            "expires_at": "2026-10-25T00:00:00Z"})
        self.assertEqual(201, status)
        status, payload = self.post("/duty/shifts", {
            "request_id": "s1", "site_id": "s1", "position": "rescue", "holder_actor_id": "op1",
            "starts_at": "2026-09-25T12:00:00Z", "ends_at": "2026-09-26T00:00:00Z",
            "shift_id": "shift-1"})
        self.assertEqual(201, status)
        self.assertEqual("duty_shift", payload["resource_type"])
        status, payload = self.post("/duty/shifts", {
            "request_id": "s1", "site_id": "s1", "position": "rescue", "holder_actor_id": "op1",
            "starts_at": "2026-09-25T12:00:00Z", "ends_at": "2026-09-26T00:00:00Z",
            "shift_id": "shift-1"})
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])
        status, payload = self.get("/duty/shifts/current?site_id=s1&position=rescue")
        self.assertEqual(200, status)
        self.assertEqual("op1", payload["holder_actor_id"])
        self.assertEqual("2026-09-25", payload["service_date"])

    def test_notification_and_pending_flow(self):
        self.post("/duty/authorizations", {
            "request_id": "a1", "site_id": "s1", "patient_ref": "p1", "contact_name": "家属甲",
            "contact_channel": "phone", "scope": "full", "authorization_id": "auth-1"}, actor="op1")
        status, _ = self.post("/duty/notifications", {
            "request_id": "n1", "site_id": "s1", "patient_ref": "p1", "authorization_id": "auth-1",
            "scope": "identity_only", "summary": "请到院", "notification_id": "n1",
            "window_start": "2026-09-25T08:00:00Z", "window_end": "2026-09-25T16:00:00Z"}, actor="op1")
        self.assertEqual(201, status)
        status, payload = self.get("/duty/notifications/pending?site_id=s1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        status, payload = self.post("/duty/notifications/deliver", {"notification_id": "n1"}, actor="op1")
        self.assertEqual(200, status)
        self.assertEqual("delivered", payload["status"])
        status, payload = self.get("/duty/notifications/pending?site_id=s1")
        self.assertEqual(0, len(payload["items"]))
        status, payload = self.get("/duty/disclosures?site_id=s1&patient_ref=p1")
        self.assertEqual(1, len(payload["items"]))

    def test_scope_query_requires_parameters(self):
        status, payload = self.get("/duty/authorizations/scope?site_id=s1")
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_handover_decisions_listing(self):
        self.post("/duty/qualifications", {
            "request_id": "q1", "target_actor_id": "op1", "position": "rescue",
            "expires_at": "2026-10-25T00:00:00Z"})
        self.post("/duty/shifts", {
            "request_id": "s1", "site_id": "s1", "position": "rescue", "holder_actor_id": "op1",
            "starts_at": "2026-09-25T12:00:00Z", "ends_at": "2026-09-26T00:00:00Z",
            "shift_id": "shift-1"})
        self.post("/duty/shifts", {
            "request_id": "s2", "site_id": "s1", "position": "rescue", "holder_actor_id": "op1",
            "starts_at": "2026-09-26T00:00:00Z", "ends_at": "2026-09-26T12:00:00Z",
            "shift_id": "shift-2"})
        self.post("/duty/handover-items", {
            "request_id": "i1", "site_id": "s1", "shift_id": "shift-1", "patient_ref": "p1",
            "category": "observation", "summary": "留观"}, actor="op1")
        status, payload = self.post("/duty/handovers", {
            "request_id": "h1", "from_shift_id": "shift-1", "to_shift_id": "shift-2"}, actor="op1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["decisions"]))
        status, payload = self.get("/duty/handovers/decisions?site_id=s1&batch_id=h1")
        self.assertEqual(1, len(payload["items"]))
        self.assertEqual("transferred", payload["items"][0]["decision"])


if __name__ == "__main__":
    unittest.main()
