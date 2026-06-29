"""Tiny self-test for the metric primitives. Run: `python -m smartmoney.tests`.

Builds a deterministic 5-trade wallet and asserts ROI / hold-time produce the hand-computed values.
"""
from __future__ import annotations

import pandas as pd

from . import metrics


def _trades():
    # token A: buy 100 @ 0.40, sell 100 @ 0.60 → +20 USD, hold 1 day
    # token A: buy 50 @ 0.50, hold open
    # token B: buy 200 @ 0.30, sell 200 @ 0.20 → -20 USD, hold 2 days
    # token C: buy 100 @ 0.10 — open
    base = 1_700_000_000
    return pd.DataFrame([
        dict(wallet="0xtest", condition_id="m1", token_id="A", side="BUY",
             size_shares=100, price=0.40, notional_usd=40, ts=base),
        dict(wallet="0xtest", condition_id="m1", token_id="A", side="SELL",
             size_shares=100, price=0.60, notional_usd=60, ts=base + 86400),
        dict(wallet="0xtest", condition_id="m1", token_id="A", side="BUY",
             size_shares=50, price=0.50, notional_usd=25, ts=base + 86400 * 2),
        dict(wallet="0xtest", condition_id="m2", token_id="B", side="BUY",
             size_shares=200, price=0.30, notional_usd=60, ts=base + 86400 * 3),
        dict(wallet="0xtest", condition_id="m2", token_id="B", side="SELL",
             size_shares=200, price=0.20, notional_usd=40, ts=base + 86400 * 5),
        dict(wallet="0xtest", condition_id="m3", token_id="C", side="BUY",
             size_shares=100, price=0.10, notional_usd=10, ts=base + 86400 * 6),
    ])


def _markets():
    return pd.DataFrame([
        dict(condition_id="m1", resolution=None, yes_token_id="A", no_token_id="A_no", liquidity_usd=10000),
        dict(condition_id="m2", resolution=None, yes_token_id="B", no_token_id="B_no", liquidity_usd=5000),
        dict(condition_id="m3", resolution="YES", yes_token_id="C", no_token_id="C_no", liquidity_usd=2000),
    ])


def test_roi():
    trades = _trades()
    mkt = _markets()
    realized, open_pnl, notional = metrics.realized_and_open_pnl(trades, mkt)
    # Realized: A +20, B -20 → 0
    # Open: A 50 lot @ 0.50, last price 0.50 → 0; C 100 @ 0.10, payoff 1.0 → +90
    # Notional bought: 40 + 25 + 60 + 10 = 135
    assert abs(realized - 0) < 1e-6, f"realized={realized}"
    assert abs(open_pnl - 90) < 1e-6, f"open_pnl={open_pnl}"
    assert abs(notional - 135) < 1e-6
    r = metrics.roi(trades, mkt)
    assert abs(r - 90 / 135) < 1e-6, f"roi={r}"
    print("✓ roi")


def test_avg_hold():
    trades = _trades()
    # closed lots: A 100 shares held 86400s, B 200 shares held 86400*2 = 172800s
    # weighted by matched shares: (86400*100 + 172800*200) / 300 = 144000
    h = metrics.avg_holding_time_secs(trades)
    expected = (86400 * 100 + 172800 * 200) / 300
    assert abs(h - expected) < 1, f"hold={h} expected={expected}"
    print("✓ avg_hold")


def test_daily_pnl_and_dd():
    trades = _trades()
    series = metrics.daily_pnl_series(trades)
    # day +1 (sell A): +20; day +5 (sell B): -20
    assert abs(series.sum() - 0) < 1e-6
    dd = metrics.max_drawdown(trades)
    # Cum peak hit at day+1 (+20), then drops to 0 at day+5 → drawdown of $20
    # normalized by cumulative buys at day+5 = 40 + 25 + 60 = 125
    expected_dd = 20 / 125
    assert dd is not None
    assert abs(dd - expected_dd) < 1e-3, f"dd={dd} expected={expected_dd}"
    print("✓ max_dd")


if __name__ == "__main__":
    test_roi()
    test_avg_hold()
    test_daily_pnl_and_dd()
    print("all metric tests passed")
