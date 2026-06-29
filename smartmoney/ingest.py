from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Iterable

from . import clients
from .config import (
    DISCOVERY_MIN_NOTIONAL_USD,
    LEADERBOARD_PERIODS,
    TOP_N_PER_PERIOD,
)
from .db import connect, tx

log = logging.getLogger(__name__)


# ---------- wallet discovery ----------


def refresh_leaderboard() -> int:
    """Upsert top-N from each leaderboard period into wallets. Returns count of new wallets."""
    now = int(time.time())
    new_count = 0
    with connect() as conn:
        for period in LEADERBOARD_PERIODS:
            try:
                rows = clients.fetch_leaderboard(period, limit=TOP_N_PER_PERIOD)
            except Exception as e:
                log.warning("leaderboard %s failed: %s", period, e)
                continue
            with tx(conn):
                for r in rows:
                    addr = (
                        r.get("proxyWallet")
                        or r.get("address")
                        or r.get("user")
                        or r.get("wallet")
                    )
                    if not addr:
                        continue
                    addr = addr.lower()
                    name = r.get("name") or r.get("displayName") or r.get("username")
                    cur = conn.execute("SELECT address FROM wallets WHERE address = ?", (addr,))
                    if cur.fetchone() is None:
                        conn.execute(
                            "INSERT INTO wallets (address, first_seen_ts, source, display_name, active) "
                            "VALUES (?, ?, ?, ?, 1)",
                            (addr, now, f"leaderboard_{period}", name),
                        )
                        new_count += 1
                    elif name:
                        conn.execute(
                            "UPDATE wallets SET display_name = COALESCE(display_name, ?) WHERE address = ?",
                            (name, addr),
                        )
    log.info("refresh_leaderboard: %d new wallets", new_count)
    return new_count


def discover_from_large_trades(min_notional_usd: float = DISCOVERY_MIN_NOTIONAL_USD) -> int:
    """Promote unknown wallets seen in trades > threshold over last 24h."""
    cutoff = int(time.time()) - 86400
    now = int(time.time())
    added = 0
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT t.wallet FROM trades t
            LEFT JOIN wallets w ON w.address = t.wallet
            WHERE t.ts >= ? AND t.notional_usd >= ? AND w.address IS NULL
            """,
            (cutoff, min_notional_usd),
        ).fetchall()
        with tx(conn):
            for r in rows:
                conn.execute(
                    "INSERT OR IGNORE INTO wallets (address, first_seen_ts, source, active) "
                    "VALUES (?, ?, 'discovery_large_trade', 1)",
                    (r["wallet"], now),
                )
                added += 1
    log.info("discover_from_large_trades: %d new", added)
    return added


# ---------- trades ingestion ----------


def _parse_trade(wallet: str, raw: dict, ingested_ts: int) -> dict | None:
    """Normalize Data API trade to our schema. Returns None if unparseable."""
    tx_hash = raw.get("transactionHash") or raw.get("txHash") or raw.get("hash")
    log_index = raw.get("logIndex") or raw.get("log_index") or 0
    trade_id = raw.get("id") or (f"{tx_hash}:{log_index}" if tx_hash else None)
    if not trade_id:
        return None

    condition_id = raw.get("conditionId") or raw.get("condition_id")
    token_id = raw.get("asset") or raw.get("tokenId") or raw.get("token_id")
    side = (raw.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        return None

    try:
        size = float(raw.get("size") or raw.get("shares") or 0)
        price = float(raw.get("price") or 0)
    except (TypeError, ValueError):
        return None
    if size <= 0 or not (0 < price < 1):
        return None

    ts = raw.get("timestamp") or raw.get("ts") or raw.get("createdAt")
    if isinstance(ts, str):
        try:
            ts = int(float(ts))
        except ValueError:
            return None
    if ts is None:
        return None
    ts = int(ts)
    if ts > 10_000_000_000:  # ms → s
        ts //= 1000

    if not condition_id or not token_id:
        return None

    return {
        "trade_id": str(trade_id),
        "wallet": wallet.lower(),
        "condition_id": condition_id,
        "token_id": str(token_id),
        "side": side,
        "size_shares": size,
        "price": price,
        "notional_usd": size * price,
        "ts": ts,
        "tx_hash": tx_hash,
        "ingested_ts": ingested_ts,
    }


def ingest_trades(wallet: str | None = None, limit_wallets: int | None = None) -> int:
    """Incrementally pull trades for active wallets (or one specified). Returns total inserted."""
    now = int(time.time())
    inserted = 0
    with connect() as conn:
        if wallet:
            wallets = [(wallet.lower(), None)]
        else:
            q = (
                "SELECT address, last_ingested_ts FROM wallets WHERE active = 1 "
                "ORDER BY CASE WHEN last_ingested_ts IS NULL THEN 0 ELSE 1 END, last_ingested_ts ASC"
            )
            if limit_wallets:
                q += f" LIMIT {int(limit_wallets)}"
            wallets = [(r["address"], r["last_ingested_ts"]) for r in conn.execute(q)]

        for addr, since in wallets:
            try:
                wallet_inserted = 0
                latest = since or 0
                with tx(conn):
                    for raw in clients.iter_user_trades(addr, since_ts=since):
                        parsed = _parse_trade(addr, raw, now)
                        if not parsed:
                            continue
                        cur = conn.execute(
                            "INSERT OR IGNORE INTO trades "
                            "(trade_id, wallet, condition_id, token_id, side, size_shares, price, notional_usd, ts, tx_hash, ingested_ts) "
                            "VALUES (:trade_id, :wallet, :condition_id, :token_id, :side, :size_shares, :price, :notional_usd, :ts, :tx_hash, :ingested_ts)",
                            parsed,
                        )
                        if cur.rowcount:
                            wallet_inserted += 1
                            if parsed["ts"] > latest:
                                latest = parsed["ts"]
                    conn.execute(
                        "UPDATE wallets SET last_ingested_ts = ? WHERE address = ?",
                        (max(latest, since or 0, now if wallet_inserted == 0 else latest), addr),
                    )
                inserted += wallet_inserted
                if wallet_inserted:
                    log.info("wallet %s: +%d trades", addr, wallet_inserted)
            except Exception as e:
                log.warning("ingest_trades failed for %s: %s", addr, e)
    log.info("ingest_trades total: %d", inserted)
    return inserted


# ---------- markets metadata ----------


def refresh_markets(condition_ids: Iterable[str] | None = None, max_stale_secs: int = 86400) -> int:
    now = int(time.time())
    with connect() as conn:
        if condition_ids is None:
            rows = conn.execute(
                """
                SELECT DISTINCT t.condition_id FROM trades t
                LEFT JOIN markets m ON m.condition_id = t.condition_id
                WHERE m.condition_id IS NULL OR m.last_refreshed_ts < ?
                """,
                (now - max_stale_secs,),
            ).fetchall()
            ids = [r["condition_id"] for r in rows]
        else:
            ids = list(condition_ids)
        if not ids:
            return 0
        markets = clients.fetch_markets_by_condition_ids(ids)
        with tx(conn):
            for m in markets:
                cid = m.get("conditionId") or m.get("condition_id")
                if not cid:
                    continue
                clob_token_ids = m.get("clobTokenIds") or m.get("tokens") or []
                if isinstance(clob_token_ids, str):
                    import json
                    try:
                        clob_token_ids = json.loads(clob_token_ids)
                    except Exception:
                        clob_token_ids = []
                yes_tok = clob_token_ids[0] if len(clob_token_ids) > 0 else None
                no_tok = clob_token_ids[1] if len(clob_token_ids) > 1 else None
                end_iso = m.get("endDate") or m.get("end_date")
                end_ts = None
                if end_iso:
                    try:
                        from datetime import datetime
                        end_ts = int(datetime.fromisoformat(end_iso.replace("Z", "+00:00")).timestamp())
                    except Exception:
                        pass
                resolution = None
                if m.get("closed") and m.get("resolvedBy"):
                    outcomes = m.get("outcomePrices") or []
                    if isinstance(outcomes, str):
                        import json
                        try:
                            outcomes = json.loads(outcomes)
                        except Exception:
                            outcomes = []
                    if outcomes:
                        try:
                            resolution = "YES" if float(outcomes[0]) > 0.5 else "NO"
                        except (TypeError, ValueError):
                            pass
                conn.execute(
                    """
                    INSERT INTO markets (condition_id, slug, question, end_date_ts, resolution,
                        outcomes_json, yes_token_id, no_token_id, liquidity_usd, last_refreshed_ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(condition_id) DO UPDATE SET
                        slug=excluded.slug,
                        question=excluded.question,
                        end_date_ts=excluded.end_date_ts,
                        resolution=excluded.resolution,
                        outcomes_json=excluded.outcomes_json,
                        yes_token_id=excluded.yes_token_id,
                        no_token_id=excluded.no_token_id,
                        liquidity_usd=excluded.liquidity_usd,
                        last_refreshed_ts=excluded.last_refreshed_ts
                    """,
                    (
                        cid,
                        m.get("slug"),
                        m.get("question"),
                        end_ts,
                        resolution,
                        str(m.get("outcomes") or m.get("outcomePrices") or ""),
                        str(yes_tok) if yes_tok else None,
                        str(no_tok) if no_tok else None,
                        float(m.get("liquidity") or 0) or None,
                        now,
                    ),
                )
        return len(markets)


# ---------- positions ----------


def rebuild_positions(wallets: Iterable[str] | None = None) -> int:
    """Replay all trades chronologically to recompute positions per (wallet, token)."""
    now = int(time.time())
    with connect() as conn:
        if wallets is None:
            rows = conn.execute(
                "SELECT DISTINCT wallet FROM trades WHERE wallet IN (SELECT address FROM wallets WHERE active = 1)"
            ).fetchall()
            wallet_list = [r["wallet"] for r in rows]
        else:
            wallet_list = [w.lower() for w in wallets]

        updated = 0
        for addr in wallet_list:
            trades = conn.execute(
                "SELECT token_id, side, size_shares, price FROM trades WHERE wallet = ? ORDER BY ts ASC",
                (addr,),
            ).fetchall()
            agg: dict[str, dict] = defaultdict(
                lambda: {"shares": 0.0, "avg_cost": 0.0, "realized_pnl_usd": 0.0}
            )
            for t in trades:
                tok = t["token_id"]
                pos = agg[tok]
                size = float(t["size_shares"])
                price = float(t["price"])
                if t["side"] == "BUY":
                    new_shares = pos["shares"] + size
                    pos["avg_cost"] = (
                        (pos["avg_cost"] * pos["shares"] + price * size) / new_shares
                        if new_shares > 0
                        else 0.0
                    )
                    pos["shares"] = new_shares
                else:  # SELL
                    sell_size = min(size, pos["shares"]) if pos["shares"] > 0 else 0.0
                    pos["realized_pnl_usd"] += (price - pos["avg_cost"]) * sell_size
                    pos["shares"] -= size
                    if pos["shares"] <= 1e-9:
                        pos["shares"] = 0.0
                        pos["avg_cost"] = 0.0

            with tx(conn):
                conn.execute("DELETE FROM positions WHERE wallet = ?", (addr,))
                for tok, pos in agg.items():
                    if abs(pos["shares"]) < 1e-9 and abs(pos["realized_pnl_usd"]) < 1e-9:
                        continue
                    conn.execute(
                        "INSERT INTO positions (wallet, token_id, shares, avg_cost, realized_pnl_usd, last_updated_ts) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (addr, tok, pos["shares"], pos["avg_cost"], pos["realized_pnl_usd"], now),
                    )
            updated += 1
    return updated


# ---------- top-level ----------


def run_daily(limit_wallets: int | None = None) -> dict:
    stats = {}
    stats["new_leaderboard_wallets"] = refresh_leaderboard()
    stats["new_trades"] = ingest_trades(limit_wallets=limit_wallets)
    stats["discovered_wallets"] = discover_from_large_trades()
    stats["markets_refreshed"] = refresh_markets()
    stats["wallets_with_positions_rebuilt"] = rebuild_positions()
    return stats
