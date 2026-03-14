import unittest
from datetime import datetime

from market_hours import US_MARKET_TZ, get_us_market_clock


class MarketHoursTests(unittest.TestCase):
    def test_regular_session_open(self):
        # Monday 10:00 ET
        now_et = datetime(2026, 3, 16, 10, 0, 0, tzinfo=US_MARKET_TZ)
        clock = get_us_market_clock(now_et)
        self.assertTrue(clock["is_open"])
        self.assertFalse(clock["is_holiday"])

    def test_independence_day_observed_closed(self):
        # 2026-07-04 is Saturday, market holiday observed on Friday 2026-07-03
        now_et = datetime(2026, 7, 3, 10, 0, 0, tzinfo=US_MARKET_TZ)
        clock = get_us_market_clock(now_et)
        self.assertFalse(clock["is_open"])
        self.assertTrue(clock["is_holiday"])
        self.assertIn("Independence Day", clock["holiday_name"])

    def test_juneteenth_closed(self):
        # Juneteenth (regular holiday since 2022)
        now_et = datetime(2026, 6, 19, 12, 0, 0, tzinfo=US_MARKET_TZ)
        clock = get_us_market_clock(now_et)
        self.assertFalse(clock["is_open"])
        self.assertTrue(clock["is_holiday"])
        self.assertIn("Juneteenth", clock["holiday_name"])


if __name__ == "__main__":
    unittest.main()
