from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Template

from .config import POLYMARKET_ANALYTICS_WALLET_URL, REPORTS_DIR, REPORT_TOP_N
from .db import connect, tx

TEMPLATE = Template(
    """# Polymarket Smart Money — {{ as_of }}

**Top {{ top_n }} of {{ total_scored }} scored wallets.** Generated {{ generated_iso }}.

## Changes vs yesterday
{% if added %}
**ADD to PolyCop** (newly in Top {{ top_n }}):
{% for r in added %}- `{{ r.wallet_short }}` — rank #{{ r.rank }}, score {{ r.score_str }} {{ '★' * r.stars }} ([profile]({{ r.url }})){% if r.prev_rank %}, was rank #{{ r.prev_rank }}{% else %}, new entry{% endif %}
{% endfor %}
{% else %}_no additions_
{% endif %}
{% if removed %}
**REMOVE from PolyCop** (dropped from yesterday's Top {{ top_n }}):
{% for r in removed %}- `{{ r.wallet_short }}` — was rank #{{ r.prev_rank }}{% if r.rank %}, now rank #{{ r.rank }} (score {{ r.score_str }}){% else %}, no longer scored{% endif %} ([profile]({{ r.url }}))
{% endfor %}
{% else %}_no removals_
{% endif %}

## Leaderboard

| # | Wallet | Stars | Score | WoW | ROI | Sharpe | MaxDD | AvgHold | Trades | Followability |
|---|---|---|---|---|---|---|---|---|---|---|
{% for r in rows -%}
| {{ r.rank }} | [`{{ r.wallet_short }}`]({{ r.url }}){% if r.display_name %} ({{ r.display_name }}){% endif %} | {{ '★' * r.stars }}{{ '☆' * (5 - r.stars) }} | {{ r.score_str }} | {{ r.wow_str }} | {{ r.roi_str }} | {{ r.sharpe_str }} | {{ r.max_dd_str }} | {{ r.hold_str }} | {{ r.trade_count }} | {{ r.followability_str }} |
{% endfor %}

## Methodology

Score = 40% ROI + 20% Sharpe + 15% MaxDD + 15% AvgHold + 10% Early Entry,
multiplied by Confidence (log10(trades+1) / log10(1000), capped at 1.2),
plus a 0–5 pt Market Impact bonus. Clamped to 0–100.

Followability = 50% Followable ROI (delay {{ primary_delay }}s) + 20% (1 − slippage) + 15% liquidity + 15% avg holding time.

**Current Followable ROI precision: 60s / 300s / 900s** (best official API gives minute-level prices).
Tick-precision 5s / 10s / 30s will become available once the WebSocket archiver has collected ≥2 weeks of data.

Lookback window: 90 days. Min trades: 5. Wallets below show low confidence.
"""
)


def _short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


def _fmt_pct(x):
    if x is None:
        return "—"
    return f"{x * 100:+.1f}%"


def _fmt_num(x, places=2):
    if x is None:
        return "—"
    return f"{x:.{places}f}"


def _fmt_hold(secs):
    if secs is None:
        return "—"
    days = secs / 86400
    if days >= 1:
        return f"{days:.1f}d"
    hours = secs / 3600
    if hours >= 1:
        return f"{hours:.1f}h"
    return f"{secs / 60:.0f}m"


def generate(as_of: date | None = None, top_n: int = REPORT_TOP_N) -> Path:
    as_of = as_of or date.today()
    as_of_str = as_of.isoformat()
    yesterday_str = (as_of - timedelta(days=1)).isoformat()
    week_ago_str = (as_of - timedelta(days=7)).isoformat()

    with connect() as conn:
        total_scored = conn.execute(
            "SELECT COUNT(*) FROM scores WHERE as_of_date = ?", (as_of_str,)
        ).fetchone()[0]

        cur = conn.execute(
            """
            SELECT s.wallet, s.score, s.stars, s.followability, s.roi_raw, s.sharpe_raw,
                   s.max_dd_raw, s.avg_hold_secs, s.trade_count, w.display_name
            FROM scores s LEFT JOIN wallets w ON w.address = s.wallet
            WHERE s.as_of_date = ?
            ORDER BY s.score DESC
            LIMIT ?
            """,
            (as_of_str, top_n),
        )
        ranked = list(enumerate(cur.fetchall(), start=1))

        # Yesterday and last week rankings (full table, for diffing)
        prev_ranks = _ranks_on(conn, yesterday_str)
        last_week_ranks = _ranks_on(conn, week_ago_str)

        rows = []
        today_top_addresses = []
        for rank, r in ranked:
            wallet = r["wallet"]
            today_top_addresses.append(wallet)
            prev_rank = prev_ranks.get(wallet)
            week_rank = last_week_ranks.get(wallet)
            if week_rank is None:
                wow_str = "new"
            else:
                delta = week_rank - rank
                wow_str = f"{delta:+d}" if delta != 0 else "—"
            rows.append({
                "rank": rank,
                "wallet_short": _short(wallet),
                "url": POLYMARKET_ANALYTICS_WALLET_URL.format(address=wallet),
                "display_name": r["display_name"],
                "stars": r["stars"],
                "score_str": f"{r['score']:.0f}",
                "wow_str": wow_str,
                "roi_str": _fmt_pct(r["roi_raw"]),
                "sharpe_str": _fmt_num(r["sharpe_raw"]),
                "max_dd_str": _fmt_pct(-(r["max_dd_raw"] or 0)) if r["max_dd_raw"] is not None else "—",
                "hold_str": _fmt_hold(r["avg_hold_secs"]),
                "trade_count": r["trade_count"],
                "followability_str": _fmt_num(r["followability"], 0) if r["followability"] is not None else "—",
                "prev_rank": prev_rank,
            })

        prev_top_addresses = _yesterday_top_n_addresses(conn, yesterday_str)
        today_set = set(today_top_addresses)
        prev_set = set(prev_top_addresses)

        added = [
            r for r in rows
            if r["wallet_short"] in {_short(a) for a in (today_set - prev_set)}
        ]
        removed_addrs = prev_set - today_set
        removed = []
        # for removed: look up today's current rank/score if still scored
        if removed_addrs:
            placeholders = ",".join("?" for _ in removed_addrs)
            cur = conn.execute(
                f"SELECT wallet, score, stars FROM scores WHERE as_of_date = ? AND wallet IN ({placeholders})",
                (as_of_str, *removed_addrs),
            )
            still = {r["wallet"]: r for r in cur.fetchall()}
            for addr in removed_addrs:
                cur_score = still.get(addr)
                # find today's rank if present
                cur_rank = next((i for i, r in enumerate(ranked, start=1) if r["wallet"] == addr), None)
                # Need original full ranking — recompute
                full_today = conn.execute(
                    "SELECT wallet FROM scores WHERE as_of_date = ? ORDER BY score DESC",
                    (as_of_str,),
                ).fetchall()
                today_rank_map = {row["wallet"]: i for i, row in enumerate(full_today, start=1)}
                removed.append({
                    "wallet_short": _short(addr),
                    "url": POLYMARKET_ANALYTICS_WALLET_URL.format(address=addr),
                    "prev_rank": prev_top_addresses.index(addr) + 1,
                    "rank": today_rank_map.get(addr),
                    "score_str": f"{cur_score['score']:.0f}" if cur_score else "—",
                })

        # Persist run for tomorrow's diff
        with tx(conn):
            conn.execute(
                "INSERT INTO report_runs (as_of_date, generated_ts, top_n_addresses_json) VALUES (?, ?, ?) "
                "ON CONFLICT(as_of_date) DO UPDATE SET generated_ts=excluded.generated_ts, top_n_addresses_json=excluded.top_n_addresses_json",
                (as_of_str, int(time.time()), json.dumps(today_top_addresses)),
            )

    md = TEMPLATE.render(
        as_of=as_of_str,
        generated_iso=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        total_scored=total_scored,
        top_n=top_n,
        rows=rows,
        added=added,
        removed=removed,
        primary_delay=60,
    )

    path = REPORTS_DIR / f"leaderboard-{as_of_str}.md"
    path.write_text(md, encoding="utf-8")
    return path


def _ranks_on(conn, as_of_str: str) -> dict[str, int]:
    cur = conn.execute(
        "SELECT wallet FROM scores WHERE as_of_date = ? ORDER BY score DESC",
        (as_of_str,),
    )
    return {r["wallet"]: i for i, r in enumerate(cur.fetchall(), start=1)}


def _yesterday_top_n_addresses(conn, yesterday_str: str) -> list[str]:
    row = conn.execute(
        "SELECT top_n_addresses_json FROM report_runs WHERE as_of_date = ?",
        (yesterday_str,),
    ).fetchone()
    if not row:
        return []
    try:
        return json.loads(row["top_n_addresses_json"])
    except Exception:
        return []
