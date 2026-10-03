import datetime as dt
import unittest

import alert_schedule


class AlertScheduleTests(unittest.TestCase):
    def test_normal_shift_checks_next_calendar_day_at_noon(self):
        end = dt.datetime(2026, 10, 1, 18, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        self.assertEqual(alert_schedule.check_time("C", end),
                         dt.datetime(2026, 10, 2, 12, 0, tzinfo=end.tzinfo))

    def test_weekend_is_not_skipped(self):
        end = dt.datetime(2026, 10, 1, 22, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        result = alert_schedule.check_time("C", end)
        self.assertEqual(result, dt.datetime(2026, 10, 2, 12, 0, tzinfo=end.tzinfo))

    def test_overtime_argument_is_legacy_compatibility_only(self):
        shift_end = dt.datetime(2026, 10, 1, 18, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        overtime_end = dt.datetime(2026, 10, 1, 20, 0, tzinfo=shift_end.tzinfo)
        self.assertEqual(alert_schedule.check_time("C", shift_end, overtime_end=overtime_end),
                         dt.datetime(2026, 10, 2, 12, 0, tzinfo=shift_end.tzinfo))

    def test_d_requires_an_explicit_attendance_gate(self):
        end = dt.datetime(2026, 10, 1, 18, 0)
        self.assertIsNone(alert_schedule.check_time("D", end))

    def test_existing_record_never_reminds(self):
        self.assertFalse(alert_schedule.should_remind(record_found=True))
        self.assertFalse(alert_schedule.should_remind(already_reported=True))
        self.assertFalse(alert_schedule.should_remind(employee_said_not_worked=True))
        self.assertFalse(alert_schedule.should_remind(system_check_ok=False))
        self.assertTrue(alert_schedule.should_remind())


if __name__ == "__main__":
    unittest.main()
