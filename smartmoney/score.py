from __future__ import annotations

import logging
import math
import time
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import metrics
from .config import (
    CONFIDENCE_MAX,
    CONFIDENCE_TARGET_TRADES,
    FOLLOWABILITY_WEIGHTS,
    FOLLOW_DELAYS_SEC,
    LOOKBACK_DAYS,
    MARKET_IMPACT_MAX_BONUS,
    MIN_TRADES_FOR_SCORING,
    SCORE_WEIGHTS,
    STAR_BUCKETS,
)
from .db import connect, tx

log = logging.getLogger(__name__)


def _stars(score: float) -> int:
    s = 0
    for threshold in STAR_BUCKETS:
        if score >= threshold:
            s += 1
    return s


def _confidence(trade_count: int) -> float:
    if trade_count <= 0:
        return 0.0
    raw = math.log10(trade_count + 1) / math.log10(CONFIDENCE_TARGET_TRADES)
    return max(0.0, min(CONFIDENCE_MAX, raw))


def _percentile_rank(s: pd.Series, reverse: bool = False) -> pd.Series:
    """Convert raw values to 0..100 percentile rank. NaN stays NaN.
    reverse=True for metrics where lower is better (e.g., max drawdown).
    """
    valid = s.dropna()
    if valid.empty:
        return pd.Series([np.nan] * len(s), index=s.index)
    ranks = valid.rank(pct=True, ascending=not reverse) * 100.0
    return ranks.reindex(s.index)


def _load_trades_with_markets(conn, since_ts: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    trades = pd.read_sql_query(
        """
        SELECT wallet, condition_id, token_id, side, size_shares, price, notional_usd, ts
        FROM trades WHERE ts >= ?
        """,
        conn,
        params=(since_ts,),
    )
    markets = pd.read_sql_query(
        "SELECT condition_id, resolution, yes_token_id, no_token_id, liquidity_usd FROM markets",
        conn,
    )
    return trades, markets


def compute_raw_metrics(trades: pd.DataFrame, markets: pd.DataFrame) -> pd.DataFrame:
    """For each wallet in `trades`, compute one row of raw metrics."""
    rows = []
    for wallet, wtrades in trades.groupby("wallet"):
        if len(wtrades) < MIN_TRADES_FOR_SCORING:
            continue
        cids = wtrades["condition_id"].unique()
        wmarkets = markets[markets["condition_id"].isin(cids)]
        row = {
            "wallet": wallet,
            "trade_count": len(wtrades),
            "notional_usd_total": float(wtrades["notional_usd"].sum()),
            "roi_raw": metrics.roi(wtrades, wmarkets),
            "sharpe_raw": metrics.sharpe(wtrades, wmarkets),
            "max_dd_raw": metrics.max_drawdown(wtrades, wmarkets),
            "avg_hold_secs": metrics.avg_holding_time_secs(wtrades),
            "early_entry_raw": metrics.early_entry_score(wtrades),
            "market_impact_raw": metrics.market_impact(wtrades),
            "liquidity_raw": metrics.avg_liquidity(wtrades, wmarkets),
        }
        for d in FOLLOW_DELAYS_SEC:
            r, slip = metrics.followable_roi(wtrades, wmarkets, d)
            row[f"followable_roi_{d}s"] = r
            if d == FOLLOW_DELAYS_SEC[0]:
                row["slippage_raw"] = slip
        rows.append(row)
    return pd.DataFrame(rows)


def compute_scores(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return raw
    df = raw.copy()
    df["roi_pct"] = _percentile_rank(df["roi_raw"])
    df["sharpe_pct"] = _percentile_rank(df["sharpe_raw"])
    df["max_dd_pct"] = _percentile_rank(df["max_dd_raw"], reverse=True)
    df["avg_hold_pct"] = _percentile_rank(df["avg_hold_secs"])
    df["early_entry_pct"] = _percentile_rank(df["early_entry_raw"])
    df["market_impact_pct"] = _percentile_rank(df["market_impact_raw"])
    df["liquidity_pct"] = _percentile_rank(df["liquidity_raw"])

    # Followable ROI: use the 60s delay as the primary signal for Followability
    primary_follow_col = f"followable_roi_{FOLLOW_DELAYS_SEC[0]}s"
    df["followable_roi_pct"] = _percentile_rank(df[primary_follow_col])

    # Slippage: lower is better
    df["slippage_inv_pct"] = _percentile_rank(df["slippage_raw"], reverse=True)

    # Base composite (use 50 as neutral fallback for missing metrics so a wallet isn't punished
    # for an algorithm-side data gap on a single component)
    def _w(col: str, w: float) -> pd.Series:
        return df[col].fillna(50.0) * w

    base = (
        _w("roi_pct", SCORE_WEIGHTS["roi"])
        + _w("sharpe_pct", SCORE_WEIGHTS["sharpe"])
        + _w("max_dd_pct", SCORE_WEIGHTS["max_dd"])
        + _w("avg_hold_pct", SCORE_WEIGHTS["avg_hold"])
        + _w("early_entry_pct", SCORE_WEIGHTS["early_entry"])
    )

    df["confidence_mult"] = df["trade_count"].apply(_confidence)
    impact_pct = df["market_impact_pct"].fillna(0.0) / 100.0
    df["market_impact_bonus"] = impact_pct * MARKET_IMPACT_MAX_BONUS

    df["score"] = (base * df["confidence_mult"] + df["market_impact_bonus"]).clip(0, 100)
    df["stars"] = df["score"].apply(_stars)

    # Followability (separate composite)
    df["followability"] = (
        _w("followable_roi_pct", FOLLOWABILITY_WEIGHTS["followable_roi"])
        + _w("slippage_inv_pct", FOLLOWABILITY_WEIGHTS["slippage_inv"])
        + _w("liquidity_pct", FOLLOWABILITY_WEIGHTS["liquidity"])
        + _w("avg_hold_pct", FOLLOWABILITY_WEIGHTS["avg_hold"])
    ).clip(0, 100)

    return df


def write_scores(df: pd.DataFrame, as_of: date) -> int:
    if df.empty:
        return 0
    as_of_str = as_of.isoformat()
    with connect() as conn, tx(conn):
        conn.execute("DELETE FROM scores WHERE as_of_date = ?", (as_of_str,))
        for _, r in df.iterrows():
            conn.execute(
                """
                INSERT INTO scores
                (wallet, as_of_date, score, stars, followability,
                 roi_raw, roi_pct, sharpe_raw, sharpe_pct,
                 max_dd_raw, max_dd_pct, avg_hold_secs, avg_hold_pct,
                 early_entry_raw, early_entry_pct, market_impact_bonus, confidence_mult,
                 trade_count, followable_roi_1m, followable_roi_5m, followable_roi_15m,
                 slippage_raw, liquidity_raw, notional_usd_total)
                VALUES (?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?)
                """,
                (
                    r["wallet"], as_of_str, float(r["score"]), int(r["stars"]),
                    _nz(r.get("followability")),
                    _nz(r.get("roi_raw")), _nz(r.get("roi_pct")),
                    _nz(r.get("sharpe_raw")), _nz(r.get("sharpe_pct")),
                    _nz(r.get("max_dd_raw")), _nz(r.get("max_dd_pct")),
                    _nz(r.get("avg_hold_secs")), _nz(r.get("avg_hold_pct")),
                    _nz(r.get("early_entry_raw")), _nz(r.get("early_entry_pct")),
                    _nz(r.get("market_impact_bonus")), _nz(r.get("confidence_mult")),
                    int(r["trade_count"]),
                    _nz(r.get("followable_roi_60s")),
                    _nz(r.get("followable_roi_300s")),
                    _nz(r.get("followable_roi_900s")),
                    _nz(r.get("slippage_raw")), _nz(r.get("liquidity_raw")),
                    _nz(r.get("notional_usd_total")),
                ),
            )
    return len(df)


def _nz(x):
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass
    return float(x)


def run() -> int:
    since_ts = int(time.time()) - LOOKBACK_DAYS * 86400
    today = date.today()
    with connect() as conn:
        trades, markets = _load_trades_with_markets(conn, since_ts)
    if trades.empty:
        log.warning("no trades in lookback window")
        return 0
    raw = compute_raw_metrics(trades, markets)
    scored = compute_scores(raw)
    n = write_scores(scored, today)
    log.info("scored %d wallets for %s", n, today)
    return n
