import unittest

from emergency_duty.acceptance import run


class EmergencyAcceptanceTest(unittest.TestCase):
    def test_emergency_chain_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual("2026-09-25", result["shift_date"])
        self.assertEqual("wang", result["responsible_lead"])
        self.assertEqual("n4", result["blocked"])


if __name__ == "__main__":
    unittest.main()
