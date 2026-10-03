import unittest

import service


class ServiceRecoveryTests(unittest.TestCase):
    def test_recovery_is_due_after_interval_and_not_before(self):
        self.assertFalse(service.recovery_due(100.0, 129.9, 30.0))
        self.assertTrue(service.recovery_due(100.0, 130.0, 30.0))


if __name__ == '__main__':
    unittest.main()
