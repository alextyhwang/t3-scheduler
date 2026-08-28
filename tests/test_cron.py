from datetime import datetime
import unittest

from t3_scheduler.cron import CronError, CronSchedule


class CronTests(unittest.TestCase):
    def test_lists_ranges_and_steps(self):
        schedule = CronSchedule.parse("*/15 8-10 * * 1-5")
        self.assertTrue(schedule.matches(datetime(2026, 8, 28, 8, 30)))  # Friday
        self.assertFalse(schedule.matches(datetime(2026, 8, 29, 8, 30)))  # Saturday
        self.assertFalse(schedule.matches(datetime(2026, 8, 28, 8, 31)))

    def test_sunday_can_be_seven(self):
        self.assertTrue(CronSchedule.parse("0 9 * * 7").matches(datetime(2026, 8, 30, 9, 0)))

    def test_day_and_weekday_use_cron_or_semantics(self):
        schedule = CronSchedule.parse("0 9 1 * 5")
        self.assertTrue(schedule.matches(datetime(2026, 9, 1, 9, 0)))
        self.assertTrue(schedule.matches(datetime(2026, 9, 4, 9, 0)))

    def test_invalid_expression(self):
        with self.assertRaises(CronError):
            CronSchedule.parse("60 1 * * *")


if __name__ == "__main__":
    unittest.main()
