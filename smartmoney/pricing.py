from __future__ import annotations

import logging

from . import clients
from .db import connect, tx

log = logging.getLogger(__name__)

NEAR_WINDOW_SEC = 30
WIDE_WINDOW_SEC = 300


def price_snapshot(token_id: str, target_ts: int) -> float | None:
    """Look up archived price near target_ts. Falls back to CLOB history + linear interp.

    Returns None when no data available at any resolution.
    """
    with connect() as conn:
        # 1) Exact-ish hit from archiver
        row = conn.execute(
            """
            SELECT ts, price FROM price_snapshots
            WHERE token_id = ? AND ts BETWEEN ? AND ?
            ORDER BY ABS(ts - ?) ASC LIMIT 1
            """,
            (token_id, target_ts - NEAR_WINDOW_SEC, target_ts + NEAR_WINDOW_SEC, target_ts),
        ).fetchone()
        if row:
            return float(row["price"])

        # 2) Wider window — interpolate between nearest before/after if both exist
        before = conn.execute(
            "SELECT ts, price FROM price_snapshots WHERE token_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
            (token_id, target_ts),
        ).fetchone()
        after = conn.execute(
            "SELECT ts, price FROM price_snapshots WHERE token_id = ? AND ts >= ? ORDER BY ts ASC LIMIT 1",
            (token_id, target_ts),
        ).fetchone()
        if before and after and (after["ts"] - before["ts"]) <= 2 * WIDE_WINDOW_SEC:
            return _interp(before["ts"], before["price"], after["ts"], after["price"], target_ts)
        if before and (target_ts - before["ts"]) <= WIDE_WINDOW_SEC:
            return float(before["price"])
        if after and (after["ts"] - target_ts) <= WIDE_WINDOW_SEC:
            return float(after["price"])

    # 3) Fall back to CLOB price-history. Hourly fidelity covers everything from ~recent backward
    #    (12h granularity for old resolved markets is documented but newer markets often have finer)
    try:
        hist = clients.fetch_clob_price_history(
            token_id, start_ts=target_ts - 3600 * 6, end_ts=target_ts + 3600 * 6, fidelity=60
        )
    except Exception as e:
        log.debug("clob price-history failed for %s @ %s: %s", token_id, target_ts, e)
        return None
    if not hist:
        return None

    # Cache fallback samples into snapshots so subsequent lookups are free
    with connect() as conn, tx(conn):
        for p in hist:
            t = int(p.get("t") or p.get("timestamp") or 0)
            px = p.get("p") or p.get("price")
            if not t or px is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO price_snapshots (token_id, ts, price, source) VALUES (?, ?, ?, 'clob_history')",
                (token_id, t, float(px)),
            )

    # Pick closest hist point (and interp if straddling)
    points = sorted(
        ((int(p.get("t") or p.get("timestamp") or 0), float(p.get("p") or p.get("price") or 0)) for p in hist),
        key=lambda x: x[0],
    )
    before = None
    after = None
    for t, px in points:
        if t <= target_ts:
            before = (t, px)
        elif t >= target_ts and after is None:
            after = (t, px)
            break
    if before and after:
        return _interp(before[0], before[1], after[0], after[1], target_ts)
    if before:
        return before[1]
    if after:
        return after[1]
    return None


def _interp(t0: int, p0: float, t1: int, p1: float, target: int) -> float:
    if t1 == t0:
        return float(p0)
    frac = (target - t0) / (t1 - t0)
    return float(p0 + (p1 - p0) * frac)
