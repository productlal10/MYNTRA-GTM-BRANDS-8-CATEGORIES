#!/usr/bin/env python3
"""
Unit tests for RoS Calculation and Cleaning Algorithm v2.0
Validates Section 9 Worked Examples (Examples 1-5) and Step 21 synthetic testing.
"""

import unittest
import numpy as np
from datetime import date, timedelta
from ros_engine import RosCore, load_params

class TestRosWorkedExamples(unittest.TestCase):
    def setUp(self):
        self.p = load_params()

    def test_example_1_cold_start_spike(self):
        """Example 1: Whiteboard SKUs 1 and 2 (cold start).
        Stock: 500, 470, 400, 100, 0. Outflows: 30, 70, 300, 100.
        Fallback baseline 40, spread 15, imputation 40 -> spike line 40 + 4*15 = 100.
        Day 1: 30
        Day 2: 70
        Day 3: 300 -> flagged spike -> imputed to 40
        Day 4: 100 -> <= 100 accepted -> 100
        Cleaned total = 240 units against 500 raw.
        """
        close = np.array([
            [500.0, 470.0, 400.0, 100.0, 0.0]
        ])
        T = close.shape[1]
        days = [date(2026, 10, 1) + timedelta(days=i) for i in range(T)]
        day_starts = np.array([1700000000.0 + i * 86400.0 for i in range(T + 1)])
        weekdays = np.array([d.weekday() for d in days])
        group_idx = np.array([0])

        core = RosCore(
            close_obs=close,
            ts_obs=None,
            price_obs=np.ones_like(close) * 1000,
            group_idx=group_idx,
            day_starts=day_starts,
            weekdays=weekdays,
            params=self.p,
            overrides={
                "group_stats": (40.0, 15.0, 40.0),
                "dow": False,
                "trend": False
            }
        )
        r = core.run()

        # Day 0 is first snapshot (no delta). Days 1-4 correspond to outflows 30, 70, 300, 100.
        clean_sales = r["sold"][0, 1:]
        raw_outflows = core.outflow[0, 1:]

        np.testing.assert_array_equal(raw_outflows, [30.0, 70.0, 300.0, 100.0])
        np.testing.assert_array_equal(clean_sales, [30.0, 70.0, 40.0, 100.0])
        self.assertEqual(clean_sales.sum(), 240.0)

    def test_example_2_promo_restock_pair(self):
        """Example 2: Real promo sale followed by a restock.
        Stock: 500 -> Day 5 drops by 80 (to 420). Day 9 seller adds 70 (to 490).
        Lift day (-3), late refill (-2), restore gap 10 (12.5% of drop) -> new level (-2).
        Score -7 <= 0 -> real! All 80 units kept.
        """
        # 11 days: days 0..4 stock 500, day 5 stock 420 (drop 80), days 6..8 stock 420, day 9 stock 490 (+70), day 10 stock 490
        close = np.array([
            [500.0, 500.0, 500.0, 500.0, 500.0, 420.0, 420.0, 420.0, 420.0, 490.0, 490.0]
        ])
        T = close.shape[1]
        days = [date(2026, 10, 1) + timedelta(days=i) for i in range(T)]
        day_starts = np.array([1700000000.0 + i * 86400.0 for i in range(T + 1)])
        weekdays = np.array([d.weekday() for d in days])
        group_idx = np.array([0])
        promo = np.zeros((1, T), dtype=bool)
        promo[0, 5] = True  # Day 5 is lift day

        core = RosCore(
            close_obs=close,
            ts_obs=None,
            price_obs=np.ones_like(close) * 1000,
            group_idx=group_idx,
            day_starts=day_starts,
            weekdays=weekdays,
            params=self.p,
            promo=promo,
            overrides={"group_stats": (10.0, 5.0, 10.0), "dow": False, "trend": False}
        )
        r = core.run()

        # Drop is at day 5: 80 units. Inflow is at day 9: 70 units.
        self.assertEqual(core.outflow[0, 5], 80.0)
        self.assertEqual(core.inflow[0, 9], 70.0)
        # Score is <= 0 (real)
        self.assertLessEqual(r["score"][0, 5], 0)
        self.assertEqual(r["verdict"][0, 5], 2) # V_REAL
        # All 80 units accepted
        self.assertEqual(r["sold"][0, 5], 80.0)

    def test_example_3_glitch_with_bounce_back(self):
        """Example 3: Glitch with bounce-back.
        Stock 1000. Day 3: drops to 0. Day 4: 0. Day 5: back to 1000. No promo.
        Fast refill (+2), exact restore (+3), zero landing (+2), frozen gap (+2) = Score 9 -> fake!
        1000 units flagged and imputed. Inflow marked relist.
        """
        # Days 0..2 stock 1000, Day 3 stock 0, Day 4 stock 0, Day 5 stock 1000, Days 6..7 stock 1000
        close = np.array([
            [1000.0, 1000.0, 1000.0, 0.0, 0.0, 1000.0, 1000.0, 1000.0]
        ])
        T = close.shape[1]
        days = [date(2026, 10, 1) + timedelta(days=i) for i in range(T)]
        day_starts = np.array([1700000000.0 + i * 86400.0 for i in range(T + 1)])
        weekdays = np.array([d.weekday() for d in days])
        group_idx = np.array([0])

        core = RosCore(
            close_obs=close,
            ts_obs=None,
            price_obs=np.ones_like(close) * 1000,
            group_idx=group_idx,
            day_starts=day_starts,
            weekdays=weekdays,
            params=self.p,
            overrides={"group_stats": (15.0, 5.0, 15.0), "dow": False, "trend": False}
        )
        r = core.run()

        # Day 3 drop = 1000
        self.assertEqual(core.outflow[0, 3], 1000.0)
        # Score is >= 4 -> fake
        self.assertGreaterEqual(r["score"][0, 3], 4)
        self.assertEqual(r["verdict"][0, 3], 1) # V_FAKE
        # Day 3 flagged pair_fake and imputed to baseline imputation value
        self.assertTrue(r["flag"][0, 3])
        self.assertEqual(r["sold"][0, 3], 15.0)
        # Day 5 inflow marked relist
        self.assertEqual(r["inflow_class"][0, 5], "relist")

    def test_example_4_slow_seller(self):
        """Example 4: Slow seller.
        Daily outflows <= 9: customer buys 5 units.
        Every outflow <= 9 is accepted by Step 5. Never flagged.
        """
        close = np.array([
            [100.0, 100.0, 99.0, 99.0, 99.0, 98.0, 98.0, 97.0, 97.0, 97.0, 96.0, 91.0]
        ])
        T = close.shape[1]
        days = [date(2026, 10, 1) + timedelta(days=i) for i in range(T)]
        day_starts = np.array([1700000000.0 + i * 86400.0 for i in range(T + 1)])
        weekdays = np.array([d.weekday() for d in days])
        group_idx = np.array([0])

        core = RosCore(
            close_obs=close,
            ts_obs=None,
            price_obs=np.ones_like(close) * 1000,
            group_idx=group_idx,
            day_starts=day_starts,
            weekdays=weekdays,
            params=self.p
        )
        r = core.run()

        # Check that no outflow is flagged
        self.assertFalse(np.any(r["flag"]))
        # Total clean units equal raw outflows
        self.assertEqual(np.nansum(r["sold"]), np.nansum(core.outflow))
        self.assertEqual(r["sold"][0, -1], 5.0)

    def test_example_5_small_increase_on_long_oos(self):
        """Example 5: Small increase on a long-OOS SKU.
        SKU at 0 for 40 days, then shows +5.
        Return candidate (<=9), but 0 sales in trailing 30 days -> return guard reclassifies all 5 as restock.
        """
        # 35 days at 0, day 36 goes to 5.
        close = np.zeros((1, 40))
        close[0, 36:] = 5.0
        T = close.shape[1]
        days = [date(2026, 8, 1) + timedelta(days=i) for i in range(T)]
        day_starts = np.array([1700000000.0 + i * 86400.0 for i in range(T + 1)])
        weekdays = np.array([d.weekday() for d in days])
        group_idx = np.array([0])

        core = RosCore(
            close_obs=close,
            ts_obs=None,
            price_obs=np.ones_like(close) * 1000,
            group_idx=group_idx,
            day_starts=day_starts,
            weekdays=weekdays,
            params=self.p
        )
        r = core.run()

        # Day 36: inflow is 5
        self.assertEqual(core.inflow[0, 36], 5.0)
        # Returns clean must be 0 because 0 sales in trailing 30 days
        self.assertEqual(r["returns"][0, 36], 0.0)
        # Restock units must be 5
        self.assertEqual(r["restock"][0, 36], 5.0)
        self.assertEqual(r["inflow_class"][0, 36], "restock")

if __name__ == "__main__":
    unittest.main()
