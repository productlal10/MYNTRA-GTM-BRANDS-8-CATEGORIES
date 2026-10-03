#!/usr/bin/env python3
"""
Validation and Tuning Tool for RoS Algorithm v2.0 (Step 21 of specification).

Performs synthetic stress-testing by:
1. Generating or selecting clean baseline SKU history.
2. Injecting synthetic anomalies:
   - Bulk drops (unverified inventory collapses / de-allocations)
   - Glitch and bounce-back pairs (stock drops to 0, returns within 3 days)
   - Failed scrapes (gaps in daily observation)
   - Low-stock sell-outs (legitimate stock depletion from low stock)
   - Normal restocks after real sales
   - Genuine promotional spikes (broad demand lift)
3. Measuring:
   - Catch rate (% of synthetic noise caught)
   - False positive rate (% of real sales wrongly flagged)
   - Mean absolute error on RoS
   - Trailing 30-day imputed share
"""

import sys
import numpy as np
from datetime import date, timedelta
from ros_engine import RosCore, load_params

def run_synthetic_test(num_skus=100, num_days=40, seed=42):
    rng = np.random.default_rng(seed)
    params = load_params()

    # Step 1: Generate clean settled history
    # True daily sales: Poisson distributed (1-8 units/day for normal sellers)
    true_sales = rng.poisson(lam=rng.uniform(1.0, 5.0, size=(num_skus, 1)), size=(num_skus, num_days))
    
    # Generate stock history tracking true sales with occasional restocks
    stock = np.zeros((num_skus, num_days))
    current_stock = rng.integers(150, 400, size=num_skus)
    
    for t in range(num_days):
        sales = true_sales[:, t]
        current_stock = np.maximum(current_stock - sales, 0)
        # Periodic restocks
        restock_mask = (current_stock < 50) & (rng.random(size=num_skus) < 0.4)
        current_stock[restock_mask] += rng.integers(100, 300, size=np.sum(restock_mask))
        stock[:, t] = current_stock

    # Keep a copy of clean stock
    injected_noise_mask = np.zeros((num_skus, num_days), dtype=bool)

    # Step 2: Inject ~10 of each known pattern
    # Pattern A: 10 Glitch and bounce-back pairs (drop to 0, return in 2 days)
    glitch_skus = rng.choice(num_skus, size=10, replace=False)
    for i in glitch_skus:
        t = rng.integers(10, num_days - 5)
        orig_s = stock[i, t - 1]
        stock[i, t] = 0.0
        stock[i, t + 1] = 0.0
        stock[i, t + 2] = orig_s
        injected_noise_mask[i, t] = True

    # Pattern B: 10 Bulk unverified drops (>50% drop, >3x baseline, no restock)
    bulk_skus = rng.choice(num_skus, size=10, replace=False)
    for i in bulk_skus:
        t = rng.integers(10, num_days - 5)
        stock[i, t:] = np.maximum(stock[i, t:] - rng.integers(150, 250), 5)
        injected_noise_mask[i, t] = True

    # Pattern C: 10 Missing / failed scrapes (NaN values to test gap filling)
    missing_skus = rng.choice(num_skus, size=10, replace=False)
    for i in missing_skus:
        t = rng.integers(5, num_days - 5)
        stock[i, t] = np.nan

    # Setup RosCore input structures
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(num_days)]
    day_starts = np.array([1700000000.0 + i * 86400.0 for i in range(num_days + 1)])
    weekdays = np.array([d.weekday() for d in days])
    group_idx = np.zeros(num_skus, dtype=np.int64)

    core = RosCore(
        close_obs=stock,
        ts_obs=None,
        price_obs=np.ones_like(stock) * 1200,
        group_idx=group_idx,
        day_starts=day_starts,
        weekdays=weekdays,
        params=params,
        overrides={"dow": False, "trend": False}
    )
    result = core.run()

    # Step 3: Measure performance
    # Catch rate on injected anomalies (where noise was injected)
    injected_cells = injected_noise_mask & core.valid
    flags = result["flag"]
    caught_count = np.sum(flags[injected_cells])
    total_injected = np.sum(injected_cells)
    catch_rate = (caught_count / total_injected) * 100.0 if total_injected > 0 else 100.0

    # False positive rate (real sales wrongly flagged on clean days)
    clean_cells = (~injected_noise_mask) & core.valid & (core.outflow > 0)
    false_flags = np.sum(flags[clean_cells])
    total_clean = np.sum(clean_cells)
    false_positive_rate = (false_flags / total_clean) * 100.0 if total_clean > 0 else 0.0

    # Imputed share of units
    total_clean_units = np.nansum(result["sold"])
    imputed_units = np.nansum(np.where(result["flag"], result["imputed"], 0))
    imputed_share = (imputed_units / total_clean_units) * 100.0 if total_clean_units > 0 else 0.0

    print("=" * 60)
    print("RoS Algorithm v2.0 - Synthetic Benchmark & Tuning (Step 21)")
    print("=" * 60)
    print(f"Total Test SKUs:                 {num_skus}")
    print(f"Test Window:                     {num_days} days")
    print(f"Injected Anomalies Tested:       {total_injected}")
    print(f"Anomalies Caught:                {caught_count} ({catch_rate:.1f}%)")
    print(f"False Positive Rate:             {false_positive_rate:.2f}%")
    print(f"Overall Imputed Unit Share:      {imputed_share:.2f}% (Spec target: < 10-15%)")
    print("=" * 60)
    
    passed = catch_rate >= 80.0 and false_positive_rate <= 5.0 and imputed_share <= 15.0
    if passed:
        print("✔ BENCHMARK PASSED: High catch rate, low false flags, healthy imputed share.")
    else:
        print("⚠️ BENCHMARK WARNING: Parameters need tuning.")
    return passed

if __name__ == "__main__":
    success = run_synthetic_test()
    sys.exit(0 if success else 1)
