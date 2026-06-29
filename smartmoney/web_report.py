"""Export today's leaderboard to docs/leaderboard.json for the static web UI."""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

from .db import connect

WEB_DOCS = Path(__file__).resolve().parent.parent / "docs"
JSON_PATH = WEB_DOCS / "leaderboard.json"

HISTORY_DAYS = 7


def export(as_of: date | None = None) -> Path:
    as_of = as_of or date.today()
    as_of_str = as_of.isoformat()

    with connect() as conn:
        today_rows = conn.execute(
            """
            SELECT s.wallet, s.score, s.stars, s.followability,
                   s.roi_raw, s.sharpe_raw, s.max_dd_raw, s.avg_hold_secs,
                   s.early_entry_raw, s.market_impact_bonus, s.confidence_mult,
                   s.trade_count, s.notional_usd_total,
                   s.followable_roi_1m, s.followable_roi_5m, s.followable_roi_15m,
                   s.slippage_raw, s.liquidity_raw,
                   w.display_name
            FROM scores s LEFT JOIN wallets w ON w.address = s.wallet
            WHERE s.as_of_date = ?
            ORDER BY s.score DESC
            """,
            (as_of_str,),
        ).fetchall()

        # Per-wallet history for sparkline
        wallets = [r["wallet"] for r in today_rows]
        history_map: dict[str, list[dict]] = {w: [] for w in wallets}
        if wallets:
            since = (as_of - timedelta(days=HISTORY_DAYS)).isoformat()
            placeholders = ",".join("?" for _ in wallets)
            cur = conn.execute(
                f"""
                SELECT wallet, as_of_date, score FROM scores
                WHERE wallet IN ({placeholders}) AND as_of_date >= ? AND as_of_date <= ?
                ORDER BY as_of_date ASC
                """,
                (*wallets, since, as_of_str),
            )
            for r in cur.fetchall():
                history_map[r["wallet"]].append({"d": r["as_of_date"], "s": r["score"]})

        total_wallets = conn.execute("SELECT COUNT(*) FROM wallets WHERE active = 1").fetchone()[0]
        total_trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

    payload = {
        "as_of": as_of_str,
        "generated_ts": int(time.time()),
        "total_scored": len(today_rows),
        "total_wallets": total_wallets,
        "total_trades": total_trades,
        "rows": [
            {
                "rank": i + 1,
                "wallet": r["wallet"],
                "name": r["display_name"],
                "score": _round(r["score"], 1),
                "stars": int(r["stars"]),
                "followability": _round(r["followability"], 1),
                "roi": _round(r["roi_raw"], 4),
                "sharpe": _round(r["sharpe_raw"], 2),
                "max_dd": _round(r["max_dd_raw"], 4),
                "avg_hold_secs": _round(r["avg_hold_secs"], 0),
                "early_entry": _round(r["early_entry_raw"], 3),
                "market_impact_bonus": _round(r["market_impact_bonus"], 2),
                "confidence": _round(r["confidence_mult"], 2),
                "trades": r["trade_count"],
                "notional": _round(r["notional_usd_total"], 0),
                "followable_roi_1m": _round(r["followable_roi_1m"], 4),
                "followable_roi_5m": _round(r["followable_roi_5m"], 4),
                "followable_roi_15m": _round(r["followable_roi_15m"], 4),
                "slippage": _round(r["slippage_raw"], 4),
                "liquidity": _round(r["liquidity_raw"], 0),
                "history": history_map.get(r["wallet"], []),
            }
            for i, r in enumerate(today_rows)
        ],
    }

    WEB_DOCS.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return JSON_PATH


def _round(x, places):
    if x is None:
        return None
    try:
        return round(float(x), places)
    except (TypeError, ValueError):
        return None
