"""Pure metric functions over a wallet's trades.

All functions take a pandas DataFrame `trades` with columns:
    wallet, condition_id, token_id, side ('BUY'|'SELL'), size_shares, price,
    notional_usd, ts (unix s)
and an optional `markets` DataFrame with: condition_id, resolution ('YES'|'NO'|None), yes_token_id, no_token_id.

The functions normalize YES/NO internally: a SELL of YES is treated economically as a BUY of NO at (1-p).
"""
from __future__ import annotations

import math
from collections import defaultdict, deque

import numpy as np
import pandas as pd

from . import pricing
from .config import (
    EARLY_ENTRY_LOOKAHEAD_SEC,
    EARLY_ENTRY_THRESHOLD_PCT,
    MARKET_IMPACT_LOOKAHEAD_SEC,
)


# ---------- helpers ----------


def _payoff(token_id: str, market: dict | None) -> float | None:
    """Return 0 or 1 if the token is resolved; None otherwise.

    A YES token pays 1 if market resolved YES, 0 if NO. NO token is the inverse.
    """
    if not market:
        return None
    res = market.get("resolution")
    if res is None or (isinstance(res, float) and math.isnan(res)) or res == "":
        return None
    if str(market.get("yes_token_id")) == str(token_id):
        return 1.0 if res == "YES" else 0.0
    if str(market.get("no_token_id")) == str(token_id):
        return 1.0 if res == "NO" else 0.0
    return None


def _markets_by_cid(markets: pd.DataFrame | None) -> dict[str, dict]:
    if markets is None or markets.empty:
        return {}
    return {r["condition_id"]: r.to_dict() for _, r in markets.iterrows()}


# ---------- ROI (uses realized + mark-to-payoff for resolved positions) ----------


def realized_and_open_pnl(
    trades: pd.DataFrame, markets: pd.DataFrame | None = None
) -> tuple[float, float, float]:
    """Return (realized_pnl_usd, open_pnl_usd, total_notional_buys_usd) using FIFO.

    Open positions are marked at payoff if the market resolved, else at last trade price (acceptable
    surrogate for an MVP since unresolved markets dominate flow only briefly).
    """
    mkt_map = _markets_by_cid(markets)
    realized = 0.0
    total_buy_notional = 0.0
    # per-token FIFO buy queue: deque[(remaining_shares, cost_per_share)]
    fifo: dict[str, deque] = defaultdict(deque)
    last_price: dict[str, float] = {}

    for _, t in trades.sort_values("ts").iterrows():
        tok = t["token_id"]
        size = float(t["size_shares"])
        price = float(t["price"])
        last_price[tok] = price
        if t["side"] == "BUY":
            fifo[tok].append([size, price])
            total_buy_notional += size * price
        else:  # SELL
            remaining = size
            while remaining > 1e-9 and fifo[tok]:
                lot_size, lot_cost = fifo[tok][0]
                matched = min(lot_size, remaining)
                realized += (price - lot_cost) * matched
                lot_size -= matched
                remaining -= matched
                if lot_size <= 1e-9:
                    fifo[tok].popleft()
                else:
                    fifo[tok][0][0] = lot_size

    # Open positions: mark to payoff if resolved, else to last seen price
    open_pnl = 0.0
    cid_by_tok = {}
    if markets is not None and not markets.empty:
        for _, m in markets.iterrows():
            if m.get("yes_token_id"):
                cid_by_tok[str(m["yes_token_id"])] = m["condition_id"]
            if m.get("no_token_id"):
                cid_by_tok[str(m["no_token_id"])] = m["condition_id"]
    for tok, lots in fifo.items():
        if not lots:
            continue
        cid = cid_by_tok.get(str(tok))
        market = mkt_map.get(cid) if cid else None
        payoff = _payoff(tok, market)
        mark = payoff if payoff is not None else last_price.get(tok, 0.0)
        for shares, cost in lots:
            open_pnl += (mark - cost) * shares

    return realized, open_pnl, total_buy_notional


def roi(trades: pd.DataFrame, markets: pd.DataFrame | None = None) -> float | None:
    realized, open_pnl, notional = realized_and_open_pnl(trades, markets)
    if notional <= 0:
        return None
    return (realized + open_pnl) / notional


# ---------- Sharpe (daily P&L series) ----------


def daily_pnl_series(trades: pd.DataFrame, markets: pd.DataFrame | None = None) -> pd.Series:
    """Build per-day realized P&L series using FIFO matching. Open positions not included."""
    if trades.empty:
        return pd.Series(dtype=float)
    fifo: dict[str, deque] = defaultdict(deque)
    daily: dict[str, float] = defaultdict(float)
    for _, t in trades.sort_values("ts").iterrows():
        day = pd.Timestamp(t["ts"], unit="s", tz="UTC").strftime("%Y-%m-%d")
        tok = t["token_id"]
        size = float(t["size_shares"])
        price = float(t["price"])
        if t["side"] == "BUY":
            fifo[tok].append([size, price])
        else:
            remaining = size
            while remaining > 1e-9 and fifo[tok]:
                lot_size, lot_cost = fifo[tok][0]
                matched = min(lot_size, remaining)
                daily[day] += (price - lot_cost) * matched
                lot_size -= matched
                remaining -= matched
                if lot_size <= 1e-9:
                    fifo[tok].popleft()
                else:
                    fifo[tok][0][0] = lot_size
    if not daily:
        return pd.Series(dtype=float)
    s = pd.Series(daily)
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def sharpe(trades: pd.DataFrame, markets: pd.DataFrame | None = None) -> float | None:
    s = daily_pnl_series(trades, markets)
    if len(s) < 14 or s.std() == 0 or math.isnan(s.std()):
        return None
    return float(s.mean() / s.std() * math.sqrt(365))


# ---------- Max drawdown ----------


def max_drawdown(trades: pd.DataFrame, markets: pd.DataFrame | None = None) -> float | None:
    """Return drawdown as positive number (0.30 = -30%). Based on cumulative realized P&L vs running peak,
    normalized by cumulative buy notional at the peak."""
    if trades.empty:
        return None
    s = daily_pnl_series(trades, markets)
    if s.empty:
        return None
    cum = s.cumsum()
    peak = cum.cummax()
    # Normalize by gross capital deployed up to that day
    buys = trades[trades["side"] == "BUY"].copy()
    buys["day"] = pd.to_datetime(buys["ts"], unit="s", utc=True).dt.floor("D").dt.tz_localize(None)
    cum.index = pd.to_datetime(cum.index).tz_localize(None) if cum.index.tz is None else cum.index.tz_convert(None)
    daily_buys = (
        buys.groupby("day")["notional_usd"]
        .sum()
        .cumsum()
        .reindex(cum.index, method="ffill")
        .bfill()
        .replace(0, np.nan)
    )
    dd = (cum - peak) / daily_buys
    worst = dd.min()
    if pd.isna(worst):
        return None
    return float(abs(worst))


# ---------- Average holding time ----------


def avg_holding_time_secs(trades: pd.DataFrame) -> float | None:
    """FIFO-matched holding time across all closed lots. Open lots ignored."""
    if trades.empty:
        return None
    fifo: dict[str, deque] = defaultdict(deque)
    holds = []
    weights = []
    for _, t in trades.sort_values("ts").iterrows():
        tok = t["token_id"]
        size = float(t["size_shares"])
        ts = int(t["ts"])
        if t["side"] == "BUY":
            fifo[tok].append([size, ts])
        else:
            remaining = size
            while remaining > 1e-9 and fifo[tok]:
                lot_size, lot_ts = fifo[tok][0]
                matched = min(lot_size, remaining)
                holds.append(ts - lot_ts)
                weights.append(matched)
                lot_size -= matched
                remaining -= matched
                if lot_size <= 1e-9:
                    fifo[tok].popleft()
                else:
                    fifo[tok][0][0] = lot_size
    if not holds:
        return None
    return float(np.average(holds, weights=weights))


# ---------- Early entry (market leadership) ----------


def early_entry_score(trades: pd.DataFrame) -> float | None:
    """Fraction of trades where price moved >threshold in the wallet's direction within lookahead window.

    BUY at p, price(t+lookahead) > p*(1+threshold) → +1.
    SELL at p, price(t+lookahead) < p*(1-threshold) → +1.
    Returns 0..1, weighted by notional.
    """
    if trades.empty:
        return None
    wins = 0.0
    total = 0.0
    for _, t in trades.iterrows():
        future = pricing.price_snapshot(t["token_id"], int(t["ts"]) + EARLY_ENTRY_LOOKAHEAD_SEC)
        if future is None:
            continue
        p = float(t["price"])
        notional = float(t["notional_usd"])
        total += notional
        if t["side"] == "BUY" and future > p * (1 + EARLY_ENTRY_THRESHOLD_PCT):
            wins += notional
        elif t["side"] == "SELL" and future < p * (1 - EARLY_ENTRY_THRESHOLD_PCT):
            wins += notional
    if total == 0:
        return None
    return wins / total


# ---------- Market impact ----------


def market_impact(trades: pd.DataFrame) -> float | None:
    """Average notional-weighted |price(t+60s) - price_trade|. Higher = more impact."""
    if trades.empty:
        return None
    moves = 0.0
    total = 0.0
    for _, t in trades.iterrows():
        future = pricing.price_snapshot(t["token_id"], int(t["ts"]) + MARKET_IMPACT_LOOKAHEAD_SEC)
        if future is None:
            continue
        notional = float(t["notional_usd"])
        moves += abs(future - float(t["price"])) * notional
        total += notional
    if total == 0:
        return None
    return moves / total


# ---------- Followable ROI ----------


def followable_roi(
    trades: pd.DataFrame, markets: pd.DataFrame | None, delay_sec: int
) -> tuple[float | None, float | None]:
    """Simulate copy-trading with a fixed delay. Returns (followable_roi, avg_slippage_pct).

    For each BUY at ts: fill at price(ts+delay), exit later at price(matched_sell_ts+delay).
    SELLs are treated as exits in the FIFO. Unmatched longs are marked at payoff if resolved, else
    at price(last_ts + delay).
    """
    if trades.empty:
        return None, None

    mkt_map = _markets_by_cid(markets)
    cid_by_tok: dict[str, str] = {}
    if markets is not None and not markets.empty:
        for _, m in markets.iterrows():
            if m.get("yes_token_id"):
                cid_by_tok[str(m["yes_token_id"])] = m["condition_id"]
            if m.get("no_token_id"):
                cid_by_tok[str(m["no_token_id"])] = m["condition_id"]

    fifo: dict[str, deque] = defaultdict(deque)  # (shares, fill_price_followed, fill_price_actual)
    followed_pnl = 0.0
    followed_notional = 0.0
    slippage_total = 0.0
    slippage_count = 0
    last_seen: dict[str, tuple[int, float]] = {}

    for _, t in trades.sort_values("ts").iterrows():
        tok = t["token_id"]
        ts = int(t["ts"])
        size = float(t["size_shares"])
        actual_price = float(t["price"])
        followed_price = pricing.price_snapshot(tok, ts + delay_sec)
        last_seen[tok] = (ts, actual_price)
        if followed_price is None or followed_price <= 0 or followed_price >= 1:
            continue
        # slippage: how much worse you got vs the wallet
        if t["side"] == "BUY":
            slippage_total += (followed_price - actual_price) / max(actual_price, 1e-6)
            slippage_count += 1
            fifo[tok].append([size, followed_price])
            followed_notional += size * followed_price
        else:  # SELL
            slippage_total += (actual_price - followed_price) / max(actual_price, 1e-6)
            slippage_count += 1
            remaining = size
            while remaining > 1e-9 and fifo[tok]:
                lot_size, lot_cost_followed = fifo[tok][0]
                matched = min(lot_size, remaining)
                followed_pnl += (followed_price - lot_cost_followed) * matched
                lot_size -= matched
                remaining -= matched
                if lot_size <= 1e-9:
                    fifo[tok].popleft()
                else:
                    fifo[tok][0][0] = lot_size

    # Mark remaining longs
    for tok, lots in fifo.items():
        if not lots:
            continue
        cid = cid_by_tok.get(str(tok))
        market = mkt_map.get(cid) if cid else None
        payoff = _payoff(tok, market)
        if payoff is None:
            last_ts, _ = last_seen.get(tok, (None, None))
            mark = pricing.price_snapshot(tok, last_ts + delay_sec) if last_ts else None
            if mark is None:
                continue
        else:
            mark = payoff
        for shares, cost_followed in lots:
            followed_pnl += (mark - cost_followed) * shares

    roi_val = followed_pnl / followed_notional if followed_notional > 0 else None
    avg_slippage = slippage_total / slippage_count if slippage_count > 0 else None
    return roi_val, avg_slippage


# ---------- Liquidity (avg liquidity of markets traded, $USD) ----------


def avg_liquidity(trades: pd.DataFrame, markets: pd.DataFrame | None) -> float | None:
    if markets is None or markets.empty or trades.empty:
        return None
    liq = markets.set_index("condition_id")["liquidity_usd"].dropna().to_dict()
    weights = trades.groupby("condition_id")["notional_usd"].sum()
    total_notional = 0.0
    total_liq = 0.0
    for cid, w in weights.items():
        l = liq.get(cid)
        if l is None:
            continue
        total_liq += l * w
        total_notional += w
    if total_notional == 0:
        return None
    return total_liq / total_notional
