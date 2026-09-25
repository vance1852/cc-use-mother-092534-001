import unittest

from festival_duty.acceptance import run


class DutyAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual("2026-09-25", result["service_date"])
        self.assertEqual("applied", result["swap_status"])
        self.assertEqual(2, result["shift_version"])
        self.assertEqual("dr-b", result["current_holder"])
        self.assertEqual("delivered", result["delivered_status"])
        self.assertEqual("none", result["scope_after_revoke"])
        self.assertTrue(result["blocked_after_revoke"])
        self.assertEqual(["transferred", "retained_rescue"], result["handover_decisions"])
        self.assertEqual("manual_handling", result["manual_status"])
        self.assertEqual("delivered", result["resolved_status"])
        self.assertEqual(2, result["disclosures"])
        self.assertEqual({"moved_to_manual": 1, "expired": 0}, result["recovered"])
        self.assertEqual(1, result["pending_after_restart"])


if __name__ == "__main__":
    unittest.main()
