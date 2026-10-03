#!/usr/bin/env python3
"""
RoS Calculation and Cleaning Algorithm v2.0 (spec dated 3 October 2026).

Turns daily closing-stock snapshots into cleaned daily sales units, rate of sale (RoS) and a
revenue proxy for one channel (Myntra), per category database.

    Stage A  Steps 1-4   validate scrapes, fill gaps, compute movement, mark OOS days
    Stage B  Steps 5-13  small moves, inflow classes, baselines, group-day checks, cap,
                         spike / stock-fraction, reversal pairs, second pass, imputation
    Stage C  Steps 14-15 net units, RoS, revenue (point and low)
    Stage D  Steps 16-19 daily correction, 5-day finalisation, pair locking, versioning
    Stage E  Step 20     health metrics            (Step 21: see ros_validate.py)

Implementation decisions on the spec's open items (Section 10):
  * SKU granularity: one "SKU" is one style (product_id); closing stock is the style's total
    stock (plus its raw declared-pool stock where the scraper records one).
  * Calendar day: closing stock = the last successful snapshot of the local (IST) day.
    A product absent from every snapshot of a day is a failed scrape for that day.
  * Listings whose stock is owned by another listing (shared_sku_duplicate) or that only report
    availability (availability_only) carry no unit movement and are excluded.
  * Group-day checks (lift / bulk) need at least `group_min_active` active SKUs, so a 1-3 SKU
    group cannot call its own single drop a "bulk day".
  * Pair-fake units are imputed, never zeroed (Principle 3 + worked example 3):
    sold_clean = max(accepted residual, min(imputation value, raw outflow)).
  * Imputed values are rounded to whole units (`round_imputed_units`).

Usage:
    python3 ros_engine.py --db maneet_brands_shirts            # daily correction run
    python3 ros_engine.py --all                                 # every category in categories.json
    python3 ros_engine.py --db X --full                         # recompute full history
    python3 ros_engine.py --db X --revise --from 2026-10-01 --to 2026-10-05 --reason "..."
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
import time
import uuid
import warnings
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "ros_config.json")
ENGINE_VERSION = "2.0"

# Flag reason / verdict codes used inside the numpy core.
R_NONE, R_CAP, R_EXTREME, R_SPIKE, R_SF, R_PAIR = 0, 1, 2, 3, 4, 5
REASON_NAMES = {R_NONE: None, R_CAP: "cap", R_EXTREME: "extreme", R_SPIKE: "spike",
                R_SF: "stock_fraction", R_PAIR: "pair_fake"}
V_NONE, V_FAKE, V_REAL, V_UNCERTAIN = 0, 1, 2, 3
VERDICT_NAMES = {V_NONE: None, V_FAKE: "fake", V_REAL: "real", V_UNCERTAIN: "uncertain"}
VERDICT_CODES = {v: k for k, v in VERDICT_NAMES.items() if v}


# ════════════════════════════════════════════════════════════════════════════
# Configuration
# ════════════════════════════════════════════════════════════════════════════
def load_params(db_name: Optional[str] = None, path: str = CONFIG_PATH) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    params = dict(cfg["params"])
    params["pair_weights"] = dict(params["pair_weights"])
    for key, value in (cfg.get("overrides", {}).get(db_name or "", {}) or {}).items():
        if key == "pair_weights":
            params["pair_weights"].update(value)
        else:
            params[key] = value
    # Step 18 safety rule: every pair must be fully visible when its first day is finalised.
    if params["pair_lookahead"] >= params["provisional_min_age"]:
        raise ValueError("pair_lookahead must stay below provisional_min_age (spec Step 18).")
    return params


# ════════════════════════════════════════════════════════════════════════════
# Small numeric helpers
# ════════════════════════════════════════════════════════════════════════════
def _split_int(total: float, weights: np.ndarray) -> np.ndarray:
    """Split an integer change across days by weight (largest remainder, keeps the total)."""
    w = np.asarray(weights, dtype=float)
    if w.sum() <= 0:
        w = np.ones_like(w)
    w = w / w.sum()
    mag = abs(float(total))
    raw = mag * w
    base = np.floor(raw)
    rem = int(round(mag - base.sum()))
    if rem > 0:
        order = np.argsort(-(raw - base), kind="stable")
        base[order[:rem]] += 1
    return np.sign(total) * base


def _ffill_rows(a: np.ndarray) -> np.ndarray:
    """Forward-fill NaNs along axis 1."""
    n, T = a.shape
    idx = np.where(~np.isnan(a), np.arange(T)[None, :], 0)
    np.maximum.accumulate(idx, axis=1, out=idx)
    return a[np.arange(n)[:, None], idx]


def _robust_stats_1d(vals: np.ndarray, trim: float, mad_scale: float, floor: float):
    """(median, spread, trimmed mean, count) of a 1-D sample without NaNs."""
    c = vals.size
    if c == 0:
        return np.nan, np.nan, np.nan, 0
    med = float(np.median(vals))
    spread = max(mad_scale * float(np.median(np.abs(vals - med))), floor)
    s = np.sort(vals)
    k = int(math.floor(trim * c))
    core = s[k:c - k] if c - 2 * k > 0 else s
    return med, spread, float(core.mean()), c


def _window_stats(x: np.ndarray, half: int, recent_cols: np.ndarray, trim: float,
                  mad_scale: float, floor: float, chunk: int = 3000):
    """Baseline statistics for every cell from its 2*half window excluding the cell itself.

    x: (n, T) outflows with NaN for days that are not valid baseline days.
    recent_cols: (T,) bool, days with fewer than `half` later days use the trailing window only.
    Returns median, spread (scaled MAD, floored), 10%-trimmed mean, count; each (n, T).
    """
    n, T = x.shape
    W = 2 * half + 1
    med = np.full((n, T), np.nan)
    spr = np.full((n, T), np.nan)
    tmn = np.full((n, T), np.nan)
    cnt = np.zeros((n, T), dtype=np.int32)
    for lo in range(0, n, chunk):
        hi = min(n, lo + chunk)
        pad = np.pad(x[lo:hi], ((0, 0), (half, half)), constant_values=np.nan)
        win = sliding_window_view(pad, W, axis=1).copy()          # (m, T, W)
        win[:, :, half] = np.nan                                  # exclude day t itself
        win[:, recent_cols, half + 1:] = np.nan                   # trailing window for recent days
        c = (~np.isnan(win)).sum(axis=-1)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            m = np.nanmedian(win, axis=-1)
            mad = np.nanmedian(np.abs(win - m[..., None]), axis=-1)
        s = np.sort(win, axis=-1)                                  # NaNs sort last
        cs = np.cumsum(np.nan_to_num(s), axis=-1)
        k = np.floor(trim * c).astype(np.int64)
        hi_idx = np.clip(c - k - 1, 0, W - 1)
        lo_idx = k - 1
        top = np.take_along_axis(cs, hi_idx[..., None], -1)[..., 0]
        bot = np.where(lo_idx >= 0, np.take_along_axis(cs, np.clip(lo_idx, 0, W - 1)[..., None], -1)[..., 0], 0.0)
        kept = c - 2 * k
        with np.errstate(invalid="ignore", divide="ignore"):
            t = np.where(kept > 0, (top - bot) / np.maximum(kept, 1), np.nan)
        med[lo:hi] = m
        spr[lo:hi] = np.maximum(mad_scale * mad, floor)
        tmn[lo:hi] = t
        cnt[lo:hi] = c
    spr[cnt == 0] = np.nan
    return med, spr, tmn, cnt


def _group_sum(values: np.ndarray, group_idx: np.ndarray, G: int) -> np.ndarray:
    """(G, T) column-wise sums of an (n, T) array by group (NaN counts as 0)."""
    v = np.nan_to_num(values.astype(float))
    out = np.zeros((G, v.shape[1]))
    np.add.at(out, group_idx, v)
    return out


# ════════════════════════════════════════════════════════════════════════════
# Core algorithm (pure numpy, no database)
# ════════════════════════════════════════════════════════════════════════════
class RosCore:
    """Runs Steps 1-15 on an (n SKUs x T days) panel.

    close_obs  (n, T) closing stock of the day's last successful scrape, NaN = missing (Step 1)
    ts_obs     (n, T) epoch seconds of that scrape, NaN = missing
    price_obs  (n, T) selling price on the day, NaN = unknown
    group_idx  (n,)   brand-category group index
    day_starts (T+1,) epoch seconds of each local midnight (T+1 boundaries)
    weekdays   (T,)   weekday of each day (Mon=0)
    promo      (G, T) promo calendar coverage, optional
    locks      {(i, t): (j, m, score, verdict_code)} locked pair decisions (Step 18)
    overrides  test hooks: {"group_stats": (base, spread, imp), "dow": False, "trend": False}
    """

    def __init__(self, close_obs, ts_obs, price_obs, group_idx, day_starts, weekdays, params,
                 promo=None, locks=None, overrides=None):
        self.p = params
        self.close_obs = np.asarray(close_obs, dtype=float)
        self.n, self.T = self.close_obs.shape
        self.ts_obs = np.asarray(ts_obs, dtype=float) if ts_obs is not None else None
        self.price_obs = np.asarray(price_obs, dtype=float) if price_obs is not None else np.full((self.n, self.T), np.nan)
        self.group_idx = np.asarray(group_idx, dtype=np.int64)
        self.G = int(self.group_idx.max()) + 1 if self.n else 0
        self.day_starts = np.asarray(day_starts, dtype=float) if day_starts is not None else None
        self.weekdays = np.asarray(weekdays, dtype=np.int64)
        self.promo = np.zeros((self.G, self.T), dtype=bool) if promo is None else np.asarray(promo, dtype=bool)
        self.locks = locks or {}
        self.ov = overrides or {}
        self.rng = np.random.default_rng(20261003)

    # ── Stage A ─────────────────────────────────────────────────────────────
    def prepare(self):
        """Steps 1-4: gap filling, movement, OOS days."""
        p, n, T = self.p, self.n, self.T
        obs = ~np.isnan(self.close_obs)
        close = np.where(obs, self.close_obs, np.nan)
        delta = np.full((n, T), np.nan)
        gap = np.zeros((n, T), dtype=bool)

        # Consecutive successful days: plain difference.
        cons = obs[:, 1:] & obs[:, :-1]
        d = self.close_obs[:, 1:] - self.close_obs[:, :-1]
        delta[:, 1:][cons] = d[cons]

        # Step 2: a successful day whose previous day is missing but which has an earlier
        # success ends a gap; split the change over every day of the gap.
        last_idx = np.where(obs, np.arange(T)[None, :], -1)
        np.maximum.accumulate(last_idx, axis=1, out=last_idx)
        gap_end = np.zeros((n, T), dtype=bool)
        gap_end[:, 1:] = obs[:, 1:] & ~obs[:, :-1] & (last_idx[:, :-1] >= 0)
        irregular_s = float(p["irregular_gap_hours"]) * 3600.0
        for i, t in zip(*np.nonzero(gap_end)):
            q = int(last_idx[i, t - 1])
            total = self.close_obs[i, t] - self.close_obs[i, q]
            L = t - q
            weights = np.ones(L)
            if self.ts_obs is not None and self.day_starts is not None:
                t0, t1 = self.ts_obs[i, q], self.ts_obs[i, t]
                if not (np.isnan(t0) or np.isnan(t1)) and abs((t1 - t0) - 86400.0 * L) > irregular_s:
                    # Irregular spacing: allocate by hours elapsed within each day.
                    for k, dd in enumerate(range(q + 1, t + 1)):
                        start = t0 if dd == q + 1 else self.day_starts[dd]
                        end = t1 if dd == t else self.day_starts[dd + 1]
                        weights[k] = max(end - start, 0.0)
            parts = _split_int(total, weights)
            run = self.close_obs[i, q]
            for k, dd in enumerate(range(q + 1, t + 1)):
                run = run + parts[k]
                close[i, dd] = run
                delta[i, dd] = parts[k]
                gap[i, dd] = True
        close[:, :][gap] = close[gap]

        opening = np.full((n, T), np.nan)
        opening[:, 1:] = close[:, :-1]
        valid = ~np.isnan(delta) & ~np.isnan(opening)
        delta[~valid] = np.nan
        self.obs, self.close, self.opening, self.delta = obs, close, opening, delta
        self.valid = valid
        self.gap_filled = gap & valid
        self.outflow = np.where(valid, np.maximum(-delta, 0.0), np.nan)
        self.inflow = np.where(valid, np.maximum(delta, 0.0), np.nan)
        self.is_oos = valid & (opening == 0)                       # Step 4
        self.in_stock = valid & (opening > 0)
        self.price = _ffill_rows(self.price_obs)
        self.later_days = (T - 1) - np.arange(T)
        self.recent_cols = self.later_days < p["baseline_half_window"]

    # ── Step 7 ─────────────────────────────────────────────────────────────
    def _dow_index(self, base_mask):
        p = self.p
        idx = np.ones((self.G, 7))
        if self.ov.get("dow") is False:
            return idx
        lo = max(0, self.T - 7 * int(p["dow_weeks"]))
        x = np.where(base_mask, self.outflow, np.nan)[:, lo:]
        sums = _group_sum(x, self.group_idx, self.G)
        cnts = _group_sum(~np.isnan(x), self.group_idx, self.G)
        wd = self.weekdays[lo:]
        for g in range(self.G):
            tot_s, tot_c = sums[g].sum(), cnts[g].sum()
            if tot_c <= 0 or tot_s <= 0:
                continue
            overall = tot_s / tot_c
            ok = True
            vals = np.ones(7)
            for w in range(7):
                cols = (wd == w) & (cnts[g] > 0)
                if cols.sum() < p["dow_min_dates"]:
                    ok = False
                    break
                vals[w] = (sums[g, cols].sum() / cnts[g, cols].sum()) / overall
            if ok:
                idx[g] = np.clip(vals, p["trend_min"], p["trend_max"])
        return idx

    def _own_trend(self, x, valid_mask):
        """g per recent cell from the SKU's own valid days (Step 7.5)."""
        p, n, T = self.p, self.n, self.T
        g = np.ones((n, T))
        if self.ov.get("trend") is False:
            return g
        k = int(p["trend_days"])
        cc = np.cumsum(valid_mask, axis=1)
        rank = cc - 1
        cols = np.arange(T)[None, :]
        for t in np.nonzero(self.recent_cols)[0]:
            if t == 0:
                continue
            c_t = cc[:, t - 1][:, None]
            prior = valid_mask & (cols < t)
            last = prior & (rank >= c_t - k)
            prev = prior & (rank >= c_t - 2 * k) & (rank < c_t - k)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=RuntimeWarning)
                m1 = np.nanmedian(np.where(last, x, np.nan), axis=1)
                m0 = np.nanmedian(np.where(prev, x, np.nan), axis=1)
            enough = cc[:, t - 1] >= 2 * k
            with np.errstate(invalid="ignore", divide="ignore"):
                ratio = np.where(enough & (m0 > 0) & (m1 > 0), m1 / m0, 1.0)
            g[:, t] = np.clip(np.nan_to_num(ratio, nan=1.0), p["trend_min"], p["trend_max"])
        return g

    def _group_trend(self, x, valid_mask):
        p = self.p
        g = np.ones((self.G, self.T))
        if self.ov.get("trend") is False:
            return g
        k = int(p["trend_days"])
        sums = _group_sum(np.where(valid_mask, x, np.nan), self.group_idx, self.G)
        cnts = _group_sum(valid_mask, self.group_idx, self.G)
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = np.where(cnts > 0, sums / np.maximum(cnts, 1), np.nan)
        for t in np.nonzero(self.recent_cols)[0]:
            for gi in range(self.G):
                series = mean[gi, :t]
                series = series[~np.isnan(series)]
                if series.size < 2 * k:
                    continue
                m1, m0 = np.median(series[-k:]), np.median(series[-2 * k:-k])
                if m0 > 0 and m1 > 0:
                    g[gi, t] = np.clip(m1 / m0, p["trend_min"], p["trend_max"])
        return g

    def _group_window_stats(self, x, need):
        """Pooled brand-category fallback stats for the (group, day) pairs in `need`."""
        p, H, T = self.p, int(self.p["baseline_half_window"]), self.T
        out = {}
        if self.ov.get("group_stats") is not None:
            b, s, m = self.ov["group_stats"]
            for key in need:
                out[key] = (b, s, m)
            return out
        members = {g: np.nonzero(self.group_idx == g)[0] for g in {k[0] for k in need}}
        cap = int(p["group_pool_sample"])
        for g, t in need:
            lo = max(0, t - H)
            hi = t if self.recent_cols[t] else min(T, t + H + 1)
            cols = [c for c in range(lo, hi) if c != t]
            if not cols:
                out[(g, t)] = (np.nan, np.nan, np.nan)
                continue
            vals = x[np.ix_(members[g], cols)].ravel()
            vals = vals[~np.isnan(vals)]
            if vals.size > cap:
                vals = self.rng.choice(vals, cap, replace=False)
            med, spr, tm, _ = _robust_stats_1d(vals, p["trim"], p["mad_scale"], p["spread_floor"])
            out[(g, t)] = (med, spr, tm)
        return out

    def baselines(self, flagged_prev):
        """Step 7: baseline, spread, imputation value and source for every cell."""
        p = self.p
        base_mask = self.in_stock & ~flagged_prev            # valid baseline days
        x = np.where(base_mask, self.outflow, np.nan)
        med, spr, tmn, cnt = _window_stats(x, int(p["baseline_half_window"]), self.recent_cols,
                                           p["trim"], p["mad_scale"], p["spread_floor"])
        own = cnt >= int(p["n_min"])
        need_cells = self.valid & ~own
        gi_cells = self.group_idx[:, None] * np.ones((1, self.T), dtype=np.int64)
        need = set(zip(gi_cells[need_cells].tolist(), np.nonzero(need_cells)[1].tolist()))
        gstats = self._group_window_stats(x, need)
        base = np.where(own, med, np.nan)
        spread = np.where(own, spr, np.nan)
        imp = np.where(own, tmn, np.nan)
        for (i, t) in zip(*np.nonzero(need_cells)):
            b, s, m = gstats.get((int(self.group_idx[i]), int(t)), (np.nan, np.nan, np.nan))
            base[i, t], spread[i, t], imp[i, t] = b, s, m

        # 7.4 day-of-week and 7.5 trend adjustments (baseline and imputation value only).
        dow = self._dow_index(base_mask)
        adj = dow[self.group_idx][:, self.weekdays]
        own_g = self._own_trend(x, base_mask)
        grp_g = self._group_trend(self.outflow, base_mask)[self.group_idx]
        trend = np.where(own, own_g, grp_g)
        trend[:, ~self.recent_cols] = 1.0
        adj = adj * trend
        base = np.nan_to_num(base, nan=0.0) * adj
        imp = np.nan_to_num(imp, nan=0.0) * adj
        spread = np.nan_to_num(spread, nan=p["spread_floor"])
        return base, spread, imp, own

    # ── Step 8 ─────────────────────────────────────────────────────────────
    def group_checks(self, base):
        p, G, T = self.p, self.G, self.T
        active = self.in_stock
        n_active = _group_sum(active, self.group_idx, G)
        above = active & (self.outflow > p["lift_mult"] * base)
        large = self.valid & (self.outflow > p["large_outflow_mult"] * base) & (self.outflow > p["large_outflow_min"])
        with np.errstate(invalid="ignore", divide="ignore"):
            share_lift = np.where(n_active > 0, _group_sum(above, self.group_idx, G) / np.maximum(n_active, 1), 0.0)
            share_large = np.where(n_active > 0, _group_sum(large & active, self.group_idx, G) / np.maximum(n_active, 1), 0.0)
        enough = n_active >= p["group_min_active"]
        lift = (enough & (share_lift >= p["lift_share"])) | self.promo
        bulk = np.zeros((G, T), dtype=bool)
        U = int(p["bulk_usual_days"])
        for t in range(T):
            lo = max(0, t - U)
            hist = np.where(enough[:, lo:t], share_large[:, lo:t], np.nan)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=RuntimeWarning)
                usual = np.nan_to_num(np.nanmedian(hist, axis=1), nan=0.0) if t > lo else np.zeros(G)
            thr = np.maximum(p["bulk_mult"] * usual, p["bulk_floor"])
            bulk[:, t] = enough[:, t] & ~lift[:, t] & (share_large[:, t] > thr)
        return lift, bulk, large

    # ── Step 9 ─────────────────────────────────────────────────────────────
    def caps(self, hist):
        """C per SKU = max(group p99 of clean daily outflow, 3 x SKU's own p99)."""
        p = self.p
        h = np.where(self.in_stock, hist, np.nan)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            own = np.nanpercentile(h, p["cap_group_pct"], axis=1) if self.T else np.full(self.n, np.nan)
        gp = np.full(self.G, np.nan)
        cap = int(p["group_pool_sample"]) * 5
        for g in range(self.G):
            vals = h[self.group_idx == g].ravel()
            vals = vals[~np.isnan(vals)]
            if vals.size > cap:
                vals = self.rng.choice(vals, cap, replace=False)
            if vals.size:
                gp[g] = np.percentile(vals, p["cap_group_pct"])
        C = np.fmax(gp[self.group_idx], p["cap_own_mult"] * own)
        return np.where(np.isnan(C), np.inf, C)

    # ── Step 10 helper ─────────────────────────────────────────────────────
    def _price_signals(self):
        """Price moved with the stock drop on day t and reverted within `price_revert_days`."""
        P, T = self.price, self.T
        prev = np.full_like(P, np.nan)
        prev[:, 1:] = P[:, :-1]
        moved = ~np.isnan(P) & ~np.isnan(prev) & (P != prev)
        reverted = np.zeros_like(moved)
        for j in range(1, int(self.p["price_revert_days"]) + 1):
            nxt = np.full_like(P, np.nan)
            nxt[:, :-j] = P[:, j:]
            reverted |= (nxt == prev)
        cut = moved & (P < prev)
        return moved & reverted, cut, prev

    # ── Step 11 ────────────────────────────────────────────────────────────
    def _score_pair(self, i, t, j, out, inf, lift_d, bulk_d, prev_price):
        w = self.p["pair_weights"]
        score, signals = 0, []
        gap_days = j - t
        restore_gap = abs(self.close[i, j] - self.opening[i, t])
        if gap_days <= w["fast_refill_days"]:
            score += w["fast_refill"]; signals.append("fast_refill")
        if restore_gap <= w["exact_restore_share"] * out:
            score += w["exact_restore"]; signals.append("exact_restore")
        landing = self.close[i, t]
        if landing == 0 or (landing % w["round_multiple"] == 0):
            score += w["zero_round_landing"]; signals.append("zero_round_landing")
        if gap_days >= 2:
            between = self.close[i, t + 1:j]
            if np.all(between == landing):
                score += w["frozen_gap"]; signals.append("frozen_gap")
            bo = self.outflow[i, t + 1:j]
            if np.any((bo >= 1) & (bo <= self.p["small_move_max"])):
                score += w["sales_during_gap"]; signals.append("sales_during_gap")
        if bulk_d:
            score += w["bulk_day"]; signals.append("bulk_day")
        pt, pp, pj = self.price[i, t], prev_price[i, t], self.price[i, j]
        if not (np.isnan(pt) or np.isnan(pp)):
            if pt != pp and not np.isnan(pj) and pj == pp:
                score += w["price_glitch"]; signals.append("price_glitch")
            if pt < pp:
                score += w["price_cut"]; signals.append("price_cut")
        if lift_d:
            score += w["lift_day"]; signals.append("lift_day")
        if restore_gap > w["new_level_share"] * out:
            score += w["new_level"]; signals.append("new_level")
        if gap_days >= w["late_refill_days"]:
            score += w["late_refill"]; signals.append("late_refill")
        if score >= self.p["pair_fake_min"]:
            verdict = V_FAKE
        elif score <= self.p["pair_real_max"]:
            verdict = V_REAL
        else:
            verdict = V_UNCERTAIN
        return int(score), verdict, signals

    def pairs(self, large, lift_cells, bulk_cells, prev_price):
        p = self.p
        n, T = self.n, self.T
        verdict = np.zeros((n, T), dtype=np.int8)
        score = np.full((n, T), np.nan)
        matched = np.zeros((n, T))
        partner = np.full((n, T), -1, dtype=np.int64)        # drop -> inflow day / inflow -> drop day
        locked = np.zeros((n, T), dtype=bool)
        used = np.zeros((n, T), dtype=bool)
        details = {}
        # Locked decisions first, so their inflows cannot be claimed by another drop.
        for (i, t), (j, m, sc, vc) in self.locks.items():
            if 0 <= t < T:
                verdict[i, t], score[i, t], matched[i, t], locked[i, t] = vc, sc, m, True
                partner[i, t] = j
                if 0 <= j < T:
                    used[i, j] = True
                    partner[i, j] = t
                    verdict[i, j], score[i, j], locked[i, j] = vc, sc, True
        cells = sorted(zip(*np.nonzero(large & ~locked)), key=lambda c: (c[1], c[0]))
        L = int(p["pair_lookahead"])
        for i, t in cells:
            out = self.outflow[i, t]
            best, best_diff = -1, None
            for j in range(t + 1, min(T, t + L + 1)):
                if used[i, j] or not self.valid[i, j]:
                    continue
                inf = self.inflow[i, j]
                if inf >= p["pair_min_inflow"] and inf >= p["pair_min_inflow_share"] * out:
                    diff = abs(inf - out)
                    if best_diff is None or diff < best_diff:
                        best, best_diff = j, diff
            if best < 0:
                continue
            j = best
            sc, vc, sig = self._score_pair(i, t, j, out, self.inflow[i, j], lift_cells[i, t], bulk_cells[i, t], prev_price)
            m = min(out, self.inflow[i, j])
            verdict[i, t] = verdict[i, j] = vc
            score[i, t] = score[i, j] = sc
            matched[i, t] = m
            partner[i, t], partner[i, j] = j, t
            used[i, j] = True
            details[(int(i), int(t))] = sig
        return verdict, score, matched, partner, locked, details

    # ── Steps 8-11 for one pass + Step 13 ─────────────────────────────────
    def run_pass(self, flagged_prev, cap_hist):
        p = self.p
        base, spread, imp, own = self.baselines(flagged_prev)
        lift, bulk, large = self.group_checks(base)
        lift_c = lift[self.group_idx]
        bulk_c = bulk[self.group_idx]
        C = self.caps(cap_hist)[:, None] * np.ones((1, self.T))
        out = self.outflow
        big = self.valid & (out > p["small_move_max"])               # Step 5: <= 9 always accepted
        k = np.where(bulk_c, p["spike_k_bulk"], p["spike_k"])
        price_sig, price_cut, prev_price = self._price_signals()

        def stat_flags(o):
            o_big = self.valid & (o > p["small_move_max"])
            extreme = o_big & lift_c & (o > p["extreme_mult"] * C)
            cap = o_big & ~lift_c & (o > C)
            spike = o_big & ~lift_c & (o > base + k * spread)
            sf = (o_big & ~lift_c & (o >= p["stock_fraction"] * self.opening)
                  & (o > p["large_outflow_mult"] * base) & (bulk_c | price_sig))
            return extreme, cap, spike, sf

        extreme, cap, spike, sf = stat_flags(out)
        verdict, score, matched, partner, locked, details = self.pairs(large, lift_c, bulk_c, prev_price)
        drop_cells = (partner >= 0) & big & (matched > 0)
        fake = drop_cells & (verdict == V_FAKE)
        real = drop_cells & (verdict == V_REAL)
        uncertain = drop_cells & (verdict == V_UNCERTAIN)

        imp_units = np.round(imp) if p["round_imputed_units"] else imp
        imputed = np.minimum(np.maximum(imp_units, 0.0), np.nan_to_num(out))

        flag_stat = (extreme | cap | ((spike | sf) & ~real)) & ~fake
        reason = np.zeros((self.n, self.T), dtype=np.int8)
        reason[sf & flag_stat] = R_SF
        reason[spike & flag_stat] = R_SPIKE
        reason[cap & flag_stat] = R_CAP
        reason[extreme & flag_stat] = R_EXTREME

        sold = np.where(self.valid, out, np.nan)
        sold = np.where(flag_stat, imputed, sold)
        accepted_residual = np.zeros((self.n, self.T))

        # Pair fake: matched units flagged; residual judged on its own (Steps 11 + 13).
        resid = np.where(fake, out - matched, 0.0)
        r_ext, r_cap, r_spk, r_sf = stat_flags(resid)
        resid_flag = fake & (resid > p["small_move_max"]) & (r_ext | r_cap | r_spk | r_sf)
        resid_ok = fake & ~resid_flag
        accepted_residual[resid_ok] = resid[resid_ok]
        sold = np.where(resid_ok, np.maximum(resid, imputed), sold)
        sold = np.where(resid_flag, imputed, sold)
        reason[fake] = R_PAIR
        flag = flag_stat | fake

        return dict(base=base, spread=spread, imp=imp, own=own, lift=lift, bulk=bulk, cap=C[:, 0],
                    flag=flag, reason=reason, sold=sold, verdict=verdict, score=score, matched=matched,
                    partner=partner, locked=locked, uncertain=uncertain, fake=fake, imputed=imputed,
                    accepted_residual=accepted_residual, pair_signals=details, large=large)

    # ── Full run ───────────────────────────────────────────────────────────
    def run(self):
        self.prepare()
        no_flags = np.zeros((self.n, self.T), dtype=bool)
        pass1 = self.run_pass(no_flags, self.outflow)                  # pass 1: cap from raw
        pass2 = self.run_pass(pass1["flag"], pass1["sold"])            # Step 12: second pass
        r = pass2
        self._returns(r)
        self._aggregate(r)
        self.result = r
        return r

    def _returns(self, r):
        """Step 6: inflow classes and the 30-day return guard."""
        p, n, T = self.p, self.n, self.T
        inflow = np.nan_to_num(self.inflow)
        relist = np.zeros((n, T), dtype=bool)
        fi, ft = np.nonzero(r["fake"])
        for i, t in zip(fi, ft):
            j = r["partner"][i, t]
            if 0 <= j < T:
                relist[i, j] = True
        cand = np.where(self.valid & ~relist & (inflow >= 1) & (inflow <= p["return_max"]), inflow, 0.0)
        sold = np.nan_to_num(r["sold"])
        ret = np.zeros((n, T))
        D = int(p["return_guard_days"])
        for t in range(T):
            lo = max(0, t - D + 1)
            allowed = sold[:, lo:t + 1].sum(axis=1) - ret[:, lo:t].sum(axis=1)
            ret[:, t] = np.minimum(cand[:, t], np.maximum(allowed, 0.0))
        restock = np.where(self.valid & ~relist, inflow - ret, 0.0)
        cls = np.full((n, T), None, dtype=object)
        cls[self.valid & (inflow > 0)] = "restock"
        cls[(ret > 0)] = "return"
        cls[relist & (inflow > 0)] = "relist"
        r["returns"] = ret
        r["restock"] = np.maximum(restock, 0.0)
        r["inflow_class"] = cls
        r["relist"] = relist

    def _aggregate(self, r):
        """Steps 14-15: net units and revenue (point and low)."""
        sold = np.nan_to_num(r["sold"])
        net = sold - r["returns"]
        price = np.nan_to_num(self.price)
        low_sold = np.where(r["flag"], r["accepted_residual"], sold)
        low_sold = np.where(r["uncertain"], np.minimum(r["imputed"], np.nan_to_num(self.outflow)), low_sold)
        r["net"] = np.where(self.valid, net, np.nan)
        r["revenue_point"] = np.where(self.valid, net * price, np.nan)
        r["revenue_low"] = np.where(self.valid, (low_sold - r["returns"]) * price, np.nan)


# ════════════════════════════════════════════════════════════════════════════
# Database layer (Stages A-E against one category database)
# ════════════════════════════════════════════════════════════════════════════
DDL = """
CREATE TABLE IF NOT EXISTS ros_daily_clean (
    product_id        BIGINT NOT NULL,
    day               DATE NOT NULL,
    brand             TEXT,
    category          TEXT,
    opening_stock     INTEGER,
    closing_stock     INTEGER,
    raw_delta         INTEGER,
    raw_outflow       INTEGER,
    raw_inflow        INTEGER,
    gap_filled        BOOLEAN DEFAULT FALSE,
    is_oos            BOOLEAN DEFAULT FALSE,
    baseline          NUMERIC(12,3),
    spread            NUMERIC(12,3),
    imputation_value  NUMERIC(12,3),
    baseline_source   TEXT,
    cap_value         NUMERIC(14,3),
    is_lift_day       BOOLEAN DEFAULT FALSE,
    is_bulk_day       BOOLEAN DEFAULT FALSE,
    flag              BOOLEAN DEFAULT FALSE,
    flag_reason       TEXT,
    inflow_class      TEXT,
    pair_id           TEXT,
    pair_score        INTEGER,
    pair_verdict      TEXT,
    pair_locked       BOOLEAN DEFAULT FALSE,
    sold_clean        NUMERIC(12,3) DEFAULT 0,
    returns_clean     NUMERIC(12,3) DEFAULT 0,
    restock_units     NUMERIC(12,3) DEFAULT 0,
    net_units         NUMERIC(12,3) DEFAULT 0,
    selling_price     NUMERIC(12,2),
    revenue_point     NUMERIC(16,2) DEFAULT 0,
    revenue_low       NUMERIC(16,2) DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'provisional',
    run_id            TEXT,
    version           INTEGER NOT NULL DEFAULT 1,
    finalised_at      TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (product_id, day)
);
CREATE INDEX IF NOT EXISTS idx_ros_daily_day ON ros_daily_clean (day);
CREATE INDEX IF NOT EXISTS idx_ros_daily_group_day ON ros_daily_clean (brand, category, day);
CREATE INDEX IF NOT EXISTS idx_ros_daily_status ON ros_daily_clean (status, day);

CREATE TABLE IF NOT EXISTS ros_daily_clean_versions (
    LIKE ros_daily_clean,
    archived_at   TIMESTAMPTZ DEFAULT NOW(),
    archive_reason TEXT,
    archive_run_id TEXT
);

CREATE TABLE IF NOT EXISTS ros_pair_locks (
    product_id     BIGINT NOT NULL,
    drop_day       DATE NOT NULL,
    inflow_day     DATE NOT NULL,
    matched_units  NUMERIC(12,3),
    score          INTEGER,
    verdict        TEXT,
    signals        TEXT,
    locked_at      TIMESTAMPTZ DEFAULT NOW(),
    run_id         TEXT,
    PRIMARY KEY (product_id, drop_day)
);

CREATE TABLE IF NOT EXISTS ros_runs (
    run_id            TEXT PRIMARY KEY,
    started_at        TIMESTAMPTZ DEFAULT NOW(),
    finished_at       TIMESTAMPTZ,
    mode              TEXT,
    engine_version    TEXT,
    latest_day        DATE,
    window_start      DATE,
    finalised_through DATE,
    did_finalise      BOOLEAN DEFAULT FALSE,
    reason            TEXT,
    params            JSONB,
    summary           JSONB
);

CREATE TABLE IF NOT EXISTS ros_health (
    run_id               TEXT NOT NULL,
    brand                TEXT NOT NULL,
    category             TEXT NOT NULL,
    window_days          INTEGER,
    sku_days             INTEGER,
    in_stock_sku_days    INTEGER,
    sold_clean           NUMERIC(14,2),
    imputed_units        NUMERIC(14,2),
    imputed_share        NUMERIC(8,4),
    gap_filled_share     NUMERIC(8,4),
    cap_flags            INTEGER,
    extreme_flags        INTEGER,
    spike_flags          INTEGER,
    stock_fraction_flags INTEGER,
    pair_fake            INTEGER,
    pairs_finalised      INTEGER,
    pairs_uncertain_final INTEGER,
    uncertain_share      NUMERIC(8,4),
    alert_level          TEXT,
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (run_id, brand, category)
);

CREATE TABLE IF NOT EXISTS ros_promo_calendar (
    id          SERIAL PRIMARY KEY,
    name        TEXT,
    start_date  DATE NOT NULL,
    end_date    DATE NOT NULL,
    brand       TEXT,          -- NULL = every brand
    category    TEXT           -- NULL = every category (platform-wide event)
);

ALTER TABLE daily_sales_analytics ADD COLUMN IF NOT EXISTS raw_units_sold INTEGER;
ALTER TABLE daily_sales_analytics ADD COLUMN IF NOT EXISTS returns_units INTEGER DEFAULT 0;
ALTER TABLE daily_sales_analytics ADD COLUMN IF NOT EXISTS revenue_low NUMERIC(14,2);
ALTER TABLE daily_sales_analytics ADD COLUMN IF NOT EXISTS ros_flag_reason TEXT;
ALTER TABLE daily_sales_analytics ADD COLUMN IF NOT EXISTS ros_status TEXT;
ALTER TABLE daily_sales_analytics ADD COLUMN IF NOT EXISTS ros_version INTEGER;
"""

OUT_COLS = ["product_id", "day", "brand", "category", "opening_stock", "closing_stock", "raw_delta",
            "raw_outflow", "raw_inflow", "gap_filled", "is_oos", "baseline", "spread", "imputation_value",
            "baseline_source", "cap_value", "is_lift_day", "is_bulk_day", "flag", "flag_reason",
            "inflow_class", "pair_id", "pair_score", "pair_verdict", "pair_locked", "sold_clean",
            "returns_clean", "restock_units", "net_units", "selling_price", "revenue_point", "revenue_low",
            "status", "run_id", "version", "finalised_at"]


def _log(msg: str):
    print(f"[ros_engine {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _dsn_for_db(db_name: str) -> Dict[str, Any]:
    """Connection settings for a category database (root .env, then category .env)."""
    env = {}
    for path in (os.path.join(ROOT, ".env"),):
        if os.path.exists(path):
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return {
        "host": os.getenv("PG_HOST") or env.get("PG_HOST", "127.0.0.1"),
        "port": int(os.getenv("PG_PORT") or env.get("PG_PORT", 5432)),
        "user": os.getenv("PG_USER") or env.get("PG_USER", "postgres"),
        "password": os.getenv("PG_PASSWORD") or env.get("PG_PASSWORD", ""),
        "dbname": db_name,
    }


def _connect(dsn: Dict[str, Any], tz: str):
    import psycopg2
    d = dict(dsn)
    d["options"] = f"-c timezone={tz}"
    conn = psycopg2.connect(**d)
    conn.autocommit = False
    return conn


class RosDatabaseRunner:
    def __init__(self, conn, db_name: str, params: Dict[str, Any]):
        self.conn = conn
        self.db_name = db_name
        self.p = params
        self.tz = params["timezone"]

    # ── helpers ────────────────────────────────────────────────────────────
    def _q(self, sql, args=None):
        cur = self.conn.cursor()
        cur.execute(sql, args or ())
        return cur

    def ensure_schema(self):
        self._q(DDL)
        self.conn.commit()

    def _has_column(self, table, column) -> bool:
        return self._q("SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name=%s AND column_name=%s",
                       (table, column)).fetchone() is not None

    def _excluded_products(self) -> set:
        if self._q("SELECT to_regclass('public.product_sizes')").fetchone()[0] is None:
            return set()
        rows = self._q("""
            SELECT product_id FROM product_sizes
            WHERE COALESCE(available, 0) = 1
            GROUP BY product_id
            HAVING BOOL_AND(inventory_quality = 'shared_sku_duplicate')
            UNION
            SELECT DISTINCT product_id FROM product_sizes WHERE inventory_quality = 'availability_only';
        """).fetchall()
        return {int(r[0]) for r in rows}

    # ── main entry ─────────────────────────────────────────────────────────
    def run(self, mode: str = "daily", revise_from: Optional[date] = None, revise_to: Optional[date] = None,
            reason: Optional[str] = None) -> Dict[str, Any]:
        t_start = time.time()
        self.ensure_schema()
        run_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
        p, tz = self.p, self.tz

        latest_day = self._q(f"SELECT MAX((snapshot_date AT TIME ZONE %s)::date) FROM daily_inventory_snapshots", (tz,)).fetchone()[0]
        first_day = self._q(f"SELECT MIN((snapshot_date AT TIME ZONE %s)::date) FROM daily_inventory_snapshots", (tz,)).fetchone()[0]
        if latest_day is None:
            return {"status": "skipped", "reason": "no snapshots"}

        have_rows = self._q("SELECT EXISTS (SELECT 1 FROM ros_daily_clean)").fetchone()[0]
        if mode in ("full", "revise") or not have_rows:
            window_start = first_day
        else:
            window_start = max(first_day, latest_day - timedelta(days=int(p["lookback_days"])))
        load_start = window_start - timedelta(days=1)          # opening stock for the first day

        self._q("INSERT INTO ros_runs (run_id, mode, engine_version, latest_day, window_start, reason, params) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (run_id, mode, ENGINE_VERSION, latest_day, window_start, reason, json.dumps(p)))
        self.conn.commit()

        panel = self._load_panel(load_start, latest_day)
        if panel is None:
            return {"status": "skipped", "reason": "no products"}
        days = panel["days"]
        T = len(days)
        day_index = {d: k for k, d in enumerate(days)}

        locks = self._load_locks(panel, day_index)
        promo = self._load_promo(panel, days)

        _log(f"{self.db_name}: {panel['n']} SKUs x {T} days ({days[0]} .. {days[-1]}), {len(panel['groups'])} brand-category groups")
        core = RosCore(panel["close"], panel["ts"], panel["price"], panel["group_idx"], panel["day_starts"],
                       np.array([d.weekday() for d in days]), p, promo=promo, locks=locks)
        r = core.run()
        _log(f"{self.db_name}: core done in {time.time() - t_start:.1f}s")

        # ── Stage D: lifecycle ────────────────────────────────────────────
        last_fin = self._q("SELECT MAX(latest_day) FROM ros_runs WHERE did_finalise").fetchone()[0]
        finalise_now = last_fin is None or (latest_day - last_fin).days >= int(p["finalise_every"])
        final_cut = latest_day - timedelta(days=int(p["provisional_min_age"]))

        existing_final = set()
        for pid, d in self._q("SELECT product_id, day FROM ros_daily_clean WHERE status='final' AND day >= %s", (window_start,)):
            existing_final.add((int(pid), d))

        rows, newly_final_pairs, write_days = self._build_rows(core, r, panel, window_start, run_id, finalise_now,
                                                               final_cut, existing_final, mode, revise_from, revise_to)
        self._upsert_rows(rows, mode, reason, run_id, revise_from, revise_to)
        self._lock_pairs(newly_final_pairs, run_id)
        wb = self._write_back(core, r, panel, write_days, mode)
        health = self._health(run_id, latest_day)

        summary = {
            "skus": int(panel["n"]), "days": T, "rows_written": len(rows),
            "units_raw": float(np.nansum(np.where(core.valid, core.outflow, 0))),
            "units_clean": float(np.nansum(np.where(core.valid, r["sold"], 0))),
            "flags": {REASON_NAMES[k]: int(((r["reason"] == k) & r["flag"]).sum()) for k in (R_CAP, R_EXTREME, R_SPIKE, R_SF, R_PAIR)},
            "pairs": {name: int(((r["verdict"] == code) & (r["partner"] >= 0) & core.valid & (core.outflow > 0)).sum())
                      for code, name in ((V_FAKE, "fake"), (V_REAL, "real"), (V_UNCERTAIN, "uncertain"))},
            "returns": float(r["returns"].sum()), "gap_filled_days": int(core.gap_filled.sum()),
            "lift_days": int(r["lift"].sum()), "bulk_days": int(r["bulk"].sum()),
            "finalised": finalise_now, "final_cut": str(final_cut), "write_back": wb,
            "health_alerts": [h for h in health if h["alert_level"] != "ok"][:20],
            "seconds": round(time.time() - t_start, 1),
        }
        self._q("UPDATE ros_runs SET finished_at=NOW(), did_finalise=%s, finalised_through=%s, summary=%s WHERE run_id=%s",
                (finalise_now, final_cut if finalise_now else None, json.dumps(summary, default=str), run_id))
        self.conn.commit()
        _log(f"{self.db_name}: raw {summary['units_raw']:.0f} -> clean {summary['units_clean']:.0f} units, "
             f"flags {summary['flags']}, pairs {summary['pairs']}, {summary['seconds']}s")
        return {"status": "ok", "run_id": run_id, **summary}

    # ── Stage A loading (Step 1) ──────────────────────────────────────────
    def _load_panel(self, load_start: date, latest_day: date):
        tz = self.tz
        has_pool = self._has_column("daily_inventory_snapshots", "pool_stock")
        pool_sql = "s.pool_stock" if has_pool else "NULL::int"
        cur = self._q(f"""
            SELECT DISTINCT ON (s.product_id, (s.snapshot_date AT TIME ZONE %s)::date)
                   s.product_id, (s.snapshot_date AT TIME ZONE %s)::date AS d, s.snapshot_date,
                   EXTRACT(EPOCH FROM s.snapshot_date)::float8, s.total_stock, {pool_sql},
                   s.selling_price, s.brand, s.category
            FROM daily_inventory_snapshots s
            WHERE s.snapshot_date >= (%s::date)::timestamp AT TIME ZONE %s
            ORDER BY s.product_id, (s.snapshot_date AT TIME ZONE %s)::date, s.snapshot_date DESC
        """, (tz, tz, load_start, tz, tz))
        data = cur.fetchall()
        if not data:
            return None
        excluded = self._excluded_products()
        days = [load_start + timedelta(days=k) for k in range((latest_day - load_start).days + 1)]
        day_index = {d: k for k, d in enumerate(days)}
        meta = {}
        for pid, brand, cat in self._q("SELECT product_id, brand, category FROM products"):
            meta[int(pid)] = (brand, cat)

        pids = sorted({int(r[0]) for r in data} - excluded)
        pidx = {pid: k for k, pid in enumerate(pids)}
        n, T = len(pids), len(days)
        close = np.full((n, T), np.nan)
        ts = np.full((n, T), np.nan)
        price = np.full((n, T), np.nan)
        snap_ts = {}
        pool_seen = np.zeros(n, dtype=bool)
        pool = np.full((n, T), np.nan)
        snap_meta = {}
        for pid, d, sdt, epoch, stock, pstock, sp, brand, cat in data:
            pid = int(pid)
            k = pidx.get(pid)
            if k is None or d not in day_index:
                continue
            t = day_index[d]
            close[k, t] = float(stock or 0)
            ts[k, t] = float(epoch)
            price[k, t] = float(sp) if sp is not None else np.nan
            snap_ts[(k, t)] = sdt
            if pstock is not None:
                pool[k, t] = float(pstock)
                pool_seen[k] = True
            snap_meta[pid] = (brand, cat)
        # Declared-pool listings: closing stock includes the raw pool; days without a recorded
        # pool are treated as failed scrapes for that SKU (gap-filled), never as a pool collapse.
        if pool_seen.any():
            rows = np.nonzero(pool_seen)[0]
            sub = pool[rows]
            close[rows] = np.where(np.isnan(sub), np.nan, close[rows] + sub)
            ts[rows] = np.where(np.isnan(sub), np.nan, ts[rows])

        groups: Dict[Tuple[str, str], int] = {}
        group_idx = np.zeros(n, dtype=np.int64)
        brands, cats = [], []
        for pid in pids:
            b, c = meta.get(pid) or snap_meta.get(pid) or (None, None)
            b, c = (b or "Unknown"), (c or "Unknown")
            brands.append(b)
            cats.append(c)
            group_idx[pidx[pid]] = groups.setdefault((b, c), len(groups))

        import zoneinfo
        zi = zoneinfo.ZoneInfo(self.tz)
        day_starts = np.array([datetime(d.year, d.month, d.day, tzinfo=zi).timestamp()
                               for d in days + [days[-1] + timedelta(days=1)]])
        return dict(n=n, days=days, pids=pids, pidx=pidx, close=close, ts=ts, price=price, snap_ts=snap_ts,
                    group_idx=group_idx, groups=groups, brands=brands, cats=cats, day_starts=day_starts)

    def _load_locks(self, panel, day_index):
        locks = {}
        for pid, dd, idd, m, sc, verdict in self._q(
                "SELECT product_id, drop_day, inflow_day, matched_units, score, verdict FROM ros_pair_locks WHERE drop_day >= %s",
                (panel["days"][0],)):
            k = panel["pidx"].get(int(pid))
            if k is None or dd not in day_index:
                continue
            locks[(k, day_index[dd])] = (day_index.get(idd, -1), float(m or 0), int(sc or 0), VERDICT_CODES.get(verdict, V_NONE))
        return locks

    def _load_promo(self, panel, days):
        G = len(panel["groups"])
        promo = np.zeros((G, len(days)), dtype=bool)
        rows = self._q("SELECT start_date, end_date, brand, category FROM ros_promo_calendar WHERE end_date >= %s AND start_date <= %s",
                       (days[0], days[-1])).fetchall()
        for start, end, brand, cat in rows:
            for (b, c), g in panel["groups"].items():
                if (brand is None or brand == b) and (cat is None or cat == c):
                    for t, d in enumerate(days):
                        if start <= d <= end:
                            promo[g, t] = True
        return promo

    # ── Output rows ────────────────────────────────────────────────────────
    def _build_rows(self, core, r, panel, window_start, run_id, finalise_now, final_cut, existing_final,
                    mode, revise_from, revise_to):
        days, pids = panel["days"], panel["pids"]
        valid = core.valid.copy()
        first_col = days.index(window_start) if window_start in days else 0
        valid[:, :first_col] = False
        rows = []
        newly_final_pairs = []
        write_days = set()
        now = datetime.now(timezone.utc)

        def f(v, nd=3):
            return None if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) else round(float(v), nd)

        for i, t in zip(*np.nonzero(valid)):
            pid, d = pids[i], days[t]
            is_existing_final = (pid, d) in existing_final
            revising = mode == "revise" and revise_from and revise_to and revise_from <= d <= revise_to
            if is_existing_final and not revising:
                continue
            becomes_final = (finalise_now and d <= final_cut) or (revising and is_existing_final)
            status = "final" if becomes_final else "provisional"
            write_days.add(d)
            j = int(r["partner"][i, t])
            pair_id = pair_verdict = None
            pair_score = None
            if j >= 0 and 0 <= j < len(days):
                drop_t, inf_t = (t, j) if j > t else (j, t)
                pair_id = f"{pid}:{days[drop_t]}:{days[inf_t]}"
                pair_verdict = VERDICT_NAMES.get(int(r["verdict"][i, t]))
                pair_score = int(r["score"][i, t]) if not np.isnan(r["score"][i, t]) else None
                if becomes_final and j > t and not r["locked"][i, t] and pair_verdict:
                    newly_final_pairs.append((pid, days[t], days[j], float(r["matched"][i, t]), pair_score, pair_verdict,
                                              ",".join(r["pair_signals"].get((int(i), int(t)), []))))
            reason = REASON_NAMES.get(int(r["reason"][i, t])) if r["flag"][i, t] else None
            g = int(core.group_idx[i])
            rows.append((
                pid, d, panel["brands"][i], panel["cats"][i],
                int(core.opening[i, t]), int(core.close[i, t]), int(core.delta[i, t]),
                int(core.outflow[i, t]), int(core.inflow[i, t]), bool(core.gap_filled[i, t]), bool(core.is_oos[i, t]),
                f(r["base"][i, t]), f(r["spread"][i, t]), f(r["imp"][i, t]),
                "own" if r["own"][i, t] else "group", f(r["cap"][i]),
                bool(r["lift"][g, t]), bool(r["bulk"][g, t]), bool(r["flag"][i, t]), reason,
                r["inflow_class"][i, t], pair_id, pair_score, pair_verdict, bool(r["locked"][i, t]),
                f(r["sold"][i, t]), f(r["returns"][i, t]), f(r["restock"][i, t]), f(r["net"][i, t]),
                f(core.price[i, t], 2), f(r["revenue_point"][i, t], 2), f(r["revenue_low"][i, t], 2),
                status, run_id, 1, now if status == "final" else None,
            ))
        return rows, newly_final_pairs, write_days

    def _upsert_rows(self, rows, mode, reason, run_id, revise_from, revise_to):
        if not rows:
            return
        cur = self.conn.cursor()
        if mode == "revise" and revise_from and revise_to:
            # Step 19: finalised values are never changed silently; archive and bump the version.
            cur.execute("""
                INSERT INTO ros_daily_clean_versions
                SELECT d.*, NOW(), %s, %s FROM ros_daily_clean d
                WHERE d.status = 'final' AND d.day BETWEEN %s AND %s
            """, (reason or "revision", run_id, revise_from, revise_to))
        cur.execute("CREATE TEMP TABLE IF NOT EXISTS _ros_stage (LIKE ros_daily_clean INCLUDING DEFAULTS) ON COMMIT DROP")
        cur.execute("TRUNCATE _ros_stage")
        buf = io.StringIO()
        for row in rows:
            buf.write("\t".join("\\N" if v is None else (("t" if v else "f") if isinstance(v, bool) else str(v).replace("\t", " ").replace("\n", " ")) for v in row))
            buf.write("\n")
        buf.seek(0)
        cur.copy_expert(f"COPY _ros_stage ({','.join(OUT_COLS)}) FROM STDIN", buf)
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in OUT_COLS if c not in ("product_id", "day", "version"))
        if mode == "revise":
            guard = "ros_daily_clean.status = 'provisional' OR ros_daily_clean.day BETWEEN %s AND %s"
            version = "version = CASE WHEN ros_daily_clean.status = 'final' THEN ros_daily_clean.version + 1 ELSE ros_daily_clean.version END"
            args = (revise_from, revise_to)
        else:
            guard = "ros_daily_clean.status = 'provisional'"
            version = "version = ros_daily_clean.version"
            args = ()
        cur.execute(f"""
            INSERT INTO ros_daily_clean ({','.join(OUT_COLS)})
            SELECT {','.join(OUT_COLS)} FROM _ros_stage
            ON CONFLICT (product_id, day) DO UPDATE SET {updates}, {version}, updated_at = NOW()
            WHERE {guard}
        """, args)
        self.conn.commit()

    def _lock_pairs(self, pairs, run_id):
        """Step 18: lock a pair's verdict when its earlier day is finalised."""
        if not pairs:
            return
        from psycopg2.extras import execute_values
        execute_values(self.conn.cursor(), """
            INSERT INTO ros_pair_locks (product_id, drop_day, inflow_day, matched_units, score, verdict, signals, run_id)
            VALUES %s ON CONFLICT (product_id, drop_day) DO NOTHING
        """, [p + (run_id,) for p in pairs])
        self.conn.commit()

    # ── Write cleaned values back into daily_sales_analytics ─────────────
    def _write_back(self, core, r, panel, write_days, mode) -> Dict[str, Any]:
        """Every dashboard reads daily_sales_analytics, so each product-day's cleaned units are
        written onto that day's last snapshot row (units of gap-filled days go to the snapshot
        that closed the gap). Earlier snapshot rows of the same day are zeroed; raw values are
        kept in raw_units_sold. Days already final are re-applied from ros_daily_clean."""
        if self._q("SELECT to_regclass('public.daily_sales_analytics')").fetchone()[0] is None:
            return {"rows": 0}
        days, pids = panel["days"], panel["pids"]
        if mode == "full" or mode == "revise":
            target_days = set(days[1:])
        else:
            target_days = set(write_days)
        if not target_days:
            return {"rows": 0}
        lo_day = min(target_days)
        stored = {}
        for row in self._q("""
                SELECT product_id, day, raw_outflow, sold_clean, returns_clean, restock_units, flag, flag_reason,
                       selling_price, revenue_low, status, version, closing_stock
                FROM ros_daily_clean WHERE day >= %s""", (lo_day - timedelta(days=int(self.p["lookback_days"])),)):
            stored[(int(row[0]), row[1])] = row

        n, T = core.n, core.T
        nxt = np.where(core.obs, np.arange(T)[None, :], T)
        nxt = np.minimum.accumulate(nxt[:, ::-1], axis=1)[:, ::-1]     # next observed day >= t
        agg: Dict[Tuple[int, int], Dict[str, Any]] = {}
        for i, t in zip(*np.nonzero(core.valid)):
            tgt = int(nxt[i, t])
            if tgt >= T or days[tgt] not in target_days:
                continue
            pid = pids[i]
            s = stored.get((pid, days[t]))
            if s is None:
                continue
            a = agg.setdefault((i, tgt), {"raw": 0.0, "sold": 0.0, "ret": 0.0, "restock": 0.0, "low": 0.0,
                                          "reasons": set(), "status": "final", "version": 1, "days": 0})
            raw, sold, ret, restock = float(s[2] or 0), float(s[3] or 0), float(s[4] or 0), float(s[5] or 0)
            a["raw"] += raw
            a["sold"] += sold
            a["ret"] += ret
            a["restock"] += restock
            a["low"] += float(s[9] or 0)
            a["days"] += 1
            if s[6] and s[7]:
                a["reasons"].add(s[7])
            if s[10] != "final":
                a["status"] = "provisional"
            a["version"] = max(a["version"], int(s[11] or 1))
            a["price"] = float(s[8] or 0)
            a["close"] = int(s[12] or 0)

        out = []
        for (i, tgt), a in agg.items():
            sdt = panel["snap_ts"].get((i, tgt))
            if sdt is None:
                continue
            units = int(round(a["sold"]))
            revenue = round(units * a["price"], 2)
            removed = max(int(round(a["raw"] - a["sold"])), 0)
            ros = round(a["sold"] / max(a["days"], 1), 4)
            if a["close"] <= 0:
                status = "OOS"
            elif removed > 0 and units == 0:
                status = "UNVERIFIED_DROP"
            elif a["restock"] > 0:
                status = "RESTOCKED"
            elif ros >= 4:
                status = "FAST_MOVER"
            elif a["close"] < 15:
                status = "LOW_STOCK"
            else:
                status = "HEALTHY"
            out.append((sdt, pids[i], days[tgt], units, revenue, int(round(a["restock"])), ros, status, removed,
                        int(round(a["raw"])), int(round(a["ret"])), round(a["low"], 2),
                        ",".join(sorted(a["reasons"])) or None, a["status"], a["version"]))
        if not out:
            return {"rows": 0}
        cur = self.conn.cursor()
        cur.execute("""CREATE TEMP TABLE _ros_wb (
            analytics_date TIMESTAMPTZ, product_id BIGINT, day DATE, units INTEGER, revenue NUMERIC(14,2),
            restock INTEGER, ros NUMERIC(8,4), stock_status TEXT, removed INTEGER, raw INTEGER, returns INTEGER,
            revenue_low NUMERIC(14,2), reason TEXT, status TEXT, version INTEGER) ON COMMIT DROP""")
        from psycopg2.extras import execute_values
        execute_values(cur, "INSERT INTO _ros_wb VALUES %s", out, page_size=5000)
        cur.execute("CREATE INDEX ON _ros_wb (product_id, day)")
        # Other snapshot rows of the same local day carry no units of their own any more.
        cur.execute("""
            UPDATE daily_sales_analytics a
            SET raw_units_sold = COALESCE(a.raw_units_sold, a.units_sold + COALESCE(a.unverified_units, 0)),
                units_sold = 0, revenue_generated = 0, unverified_units = 0, ros = 0, returns_units = 0,
                revenue_low = 0, ros_flag_reason = NULL, ros_status = w.status, ros_version = w.version
            FROM _ros_wb w
            WHERE a.product_id = w.product_id
              AND (a.analytics_date AT TIME ZONE %s)::date = w.day
              AND a.analytics_date <> w.analytics_date
        """, (self.tz,))
        zeroed = cur.rowcount
        cur.execute("""
            UPDATE daily_sales_analytics a
            SET units_sold = w.units, revenue_generated = w.revenue, stock_added = w.restock, ros = w.ros,
                stock_status = w.stock_status, unverified_units = w.removed, raw_units_sold = w.raw,
                returns_units = w.returns, revenue_low = w.revenue_low, ros_flag_reason = w.reason,
                ros_status = w.status, ros_version = w.version
            FROM _ros_wb w
            WHERE a.product_id = w.product_id AND a.analytics_date = w.analytics_date
        """)
        updated = cur.rowcount
        self.conn.commit()
        return {"rows": updated, "zeroed_same_day": zeroed, "days": len(target_days)}

    # ── Step 20: health metrics ───────────────────────────────────────────
    def _health(self, run_id, latest_day) -> List[Dict[str, Any]]:
        p = self.p
        W = int(p["health_window_days"])
        lo = latest_day - timedelta(days=W - 1)
        rows = self._q("""
            SELECT COALESCE(brand,'Unknown'), COALESCE(category,'Unknown'),
                   COUNT(*) AS sku_days,
                   COUNT(*) FILTER (WHERE NOT is_oos) AS in_stock,
                   COALESCE(SUM(sold_clean),0),
                   COALESCE(SUM(sold_clean) FILTER (WHERE flag AND flag_reason IN ('cap','extreme','spike','stock_fraction')), 0)
                     + COALESCE(SUM(sold_clean - LEAST(sold_clean, GREATEST(raw_outflow - 0, 0))) FILTER (WHERE FALSE), 0),
                   AVG(CASE WHEN gap_filled THEN 1.0 ELSE 0.0 END),
                   COUNT(*) FILTER (WHERE flag_reason = 'cap'),
                   COUNT(*) FILTER (WHERE flag_reason = 'extreme'),
                   COUNT(*) FILTER (WHERE flag_reason = 'spike'),
                   COUNT(*) FILTER (WHERE flag_reason = 'stock_fraction'),
                   COUNT(*) FILTER (WHERE flag_reason = 'pair_fake'),
                   COUNT(*) FILTER (WHERE status = 'final' AND pair_verdict IS NOT NULL AND raw_outflow > 0),
                   COUNT(*) FILTER (WHERE status = 'final' AND pair_verdict = 'uncertain' AND raw_outflow > 0),
                   COALESCE(SUM(sold_clean) FILTER (WHERE flag AND flag_reason = 'pair_fake'), 0)
            FROM ros_daily_clean
            WHERE day BETWEEN %s AND %s
            GROUP BY 1, 2
        """, (lo, latest_day)).fetchall()
        out = []
        for (b, c, skud, ins, sold, imp_stat, gap_share, capf, extf, spkf, sff, pf, pfin, punc, imp_pair) in rows:
            sold = float(sold or 0)
            imputed = float(imp_stat or 0) + float(imp_pair or 0)
            share = imputed / sold if sold > 0 else 0.0
            unc = (punc / pfin) if pfin else 0.0
            level = "alert" if share > p["imputed_alert"] else ("investigate" if share > p["imputed_investigate"] else "ok")
            out.append(dict(run_id=run_id, brand=b, category=c, window_days=W, sku_days=int(skud), in_stock_sku_days=int(ins),
                            sold_clean=round(sold, 2), imputed_units=round(imputed, 2), imputed_share=round(share, 4),
                            gap_filled_share=round(float(gap_share or 0), 4), cap_flags=int(capf), extreme_flags=int(extf),
                            spike_flags=int(spkf), stock_fraction_flags=int(sff), pair_fake=int(pf),
                            pairs_finalised=int(pfin), pairs_uncertain_final=int(punc), uncertain_share=round(unc, 4),
                            alert_level=level))
        if out:
            from psycopg2.extras import execute_values
            cols = list(out[0].keys())
            execute_values(self.conn.cursor(), f"INSERT INTO ros_health ({','.join(cols)}) VALUES %s ON CONFLICT DO NOTHING",
                           [tuple(h[c] for c in cols) for h in out])
            # Keep the last 60 runs of health history.
            self._q("""DELETE FROM ros_health WHERE run_id NOT IN
                       (SELECT run_id FROM ros_runs ORDER BY started_at DESC LIMIT 60)""")
            self.conn.commit()
        return out


# ════════════════════════════════════════════════════════════════════════════
# Entry points
# ════════════════════════════════════════════════════════════════════════════
def run_for_database(db_name: str, mode: str = "daily", dsn: Optional[Dict[str, Any]] = None,
                     revise_from: Optional[date] = None, revise_to: Optional[date] = None,
                     reason: Optional[str] = None) -> Dict[str, Any]:
    params = load_params(db_name)
    conn = _connect(dsn or _dsn_for_db(db_name), params["timezone"])
    try:
        # One RoS run per database at a time (a separate key from the snapshot lock).
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(%s)", (0x524F5332,))
        if not cur.fetchone()[0]:
            return {"status": "skipped", "reason": "another RoS run is in progress"}
        try:
            return RosDatabaseRunner(conn, db_name, params).run(mode, revise_from, revise_to, reason)
        finally:
            try:
                conn.rollback()
                conn.cursor().execute("SELECT pg_advisory_unlock(%s)", (0x524F5332,))
                conn.commit()
            except Exception:
                pass
    finally:
        conn.close()


def _category_dbs() -> List[str]:
    with open(os.path.join(ROOT, "categories.json"), encoding="utf-8") as f:
        return [c["db"] for c in json.load(f)["categories"]]


def main():
    ap = argparse.ArgumentParser(description="RoS Calculation and Cleaning Algorithm v2.0")
    ap.add_argument("--db", help="category database name")
    ap.add_argument("--all", action="store_true", help="run every category in categories.json")
    ap.add_argument("--full", action="store_true", help="recompute the full history (finals stay frozen)")
    ap.add_argument("--revise", action="store_true", help="revise finalised days (creates a new version)")
    ap.add_argument("--from", dest="date_from")
    ap.add_argument("--to", dest="date_to")
    ap.add_argument("--reason")
    a = ap.parse_args()
    dbs = _category_dbs() if a.all else ([a.db] if a.db else [])
    if not dbs:
        ap.error("pass --db NAME or --all")
    mode = "revise" if a.revise else ("full" if a.full else "daily")
    rf = date.fromisoformat(a.date_from) if a.date_from else None
    rt = date.fromisoformat(a.date_to) if a.date_to else None
    if mode == "revise" and not (rf and rt and a.reason):
        ap.error("--revise needs --from, --to and --reason")
    results = {}
    for db in dbs:
        try:
            results[db] = run_for_database(db, mode, revise_from=rf, revise_to=rt, reason=a.reason)
        except Exception as e:
            import traceback
            traceback.print_exc()
            results[db] = {"status": "error", "error": str(e)}
    print(json.dumps({db: {k: v for k, v in r.items() if k in ("status", "run_id", "units_raw", "units_clean", "flags", "pairs", "seconds", "error", "reason")}
                      for db, r in results.items()}, indent=2, default=str))


if __name__ == "__main__":
    main()
