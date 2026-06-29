"""Long-running WebSocket archiver. Subscribes to Polymarket live trades and selected orderbooks,
writes every tick into price_snapshots so later we can answer 'price at timestamp t'.

Run with: `python -m smartmoney archive`
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import time

import websockets

from .config import (
    ARCHIVER_BOOK_SAMPLE_SECS,
    ARCHIVER_FLUSH_ROWS,
    ARCHIVER_FLUSH_SECS,
    ARCHIVER_TOP_TOKENS,
    ARCHIVER_WS_PING_SECS,
    HEARTBEAT_PATH,
    WS_CLOB,
    WS_RTDS,
)
from .db import connect, tx

log = logging.getLogger(__name__)

_buffer: list[tuple[str, int, float, str]] = []
_buffer_lock = asyncio.Lock()


async def _flusher():
    while True:
        await asyncio.sleep(ARCHIVER_FLUSH_SECS)
        await _flush()


async def _flush(force: bool = False):
    async with _buffer_lock:
        if not _buffer:
            return
        if not force and len(_buffer) < ARCHIVER_FLUSH_ROWS:
            # only flush on time-tick if buffer is non-empty
            pass
        rows = list(_buffer)
        _buffer.clear()
    # synchronous DB write off the event loop
    await asyncio.get_event_loop().run_in_executor(None, _write_rows, rows)


def _write_rows(rows: list[tuple[str, int, float, str]]) -> None:
    if not rows:
        return
    with connect() as conn, tx(conn):
        conn.executemany(
            "INSERT OR IGNORE INTO price_snapshots (token_id, ts, price, source) VALUES (?, ?, ?, ?)",
            rows,
        )


async def _enqueue(token_id: str, ts: int, price: float, source: str):
    async with _buffer_lock:
        _buffer.append((str(token_id), int(ts), float(price), source))


async def _heartbeat():
    while True:
        HEARTBEAT_PATH.write_text(str(int(time.time())))
        await asyncio.sleep(30)


# ---------- RTDS trades stream ----------


async def _rtds_subscriber():
    backoff = 1
    while True:
        try:
            log.info("RTDS connecting…")
            async with websockets.connect(WS_RTDS, ping_interval=ARCHIVER_WS_PING_SECS) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "action": "subscribe",
                            "subscriptions": [{"topic": "activity", "type": "trades"}],
                        }
                    )
                )
                backoff = 1
                async for msg in ws:
                    await _handle_rtds_message(msg)
        except Exception as e:
            log.warning("RTDS error: %s — reconnecting in %ds", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def _handle_rtds_message(msg: str):
    try:
        data = json.loads(msg)
    except json.JSONDecodeError:
        return
    payload = data.get("payload") or data
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and "trades" in payload:
        items = payload["trades"]
    else:
        items = [payload]
    for it in items:
        token_id = it.get("asset") or it.get("tokenId") or it.get("token_id")
        price = it.get("price")
        ts = it.get("timestamp") or it.get("ts") or int(time.time())
        if token_id is None or price is None:
            continue
        try:
            ts = int(ts)
            if ts > 10_000_000_000:
                ts //= 1000
            await _enqueue(token_id, ts, float(price), "ws_trade")
        except (TypeError, ValueError):
            continue


# ---------- CLOB orderbook stream (top-N most-liquid tokens) ----------


def _top_tokens(limit: int) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT yes_token_id FROM markets
            WHERE yes_token_id IS NOT NULL AND liquidity_usd IS NOT NULL AND resolution IS NULL
            ORDER BY liquidity_usd DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [r["yes_token_id"] for r in rows]


async def _clob_book_subscriber():
    last_mid: dict[str, tuple[int, float]] = {}
    backoff = 1
    refresh_tokens_every = 3600
    last_refresh = 0
    tokens: list[str] = []
    while True:
        try:
            now = int(time.time())
            if not tokens or (now - last_refresh) > refresh_tokens_every:
                tokens = _top_tokens(ARCHIVER_TOP_TOKENS)
                last_refresh = now
                if not tokens:
                    log.info("no tokens to subscribe yet — waiting 60s")
                    await asyncio.sleep(60)
                    continue
            log.info("CLOB book connecting (%d tokens)…", len(tokens))
            async with websockets.connect(WS_CLOB, ping_interval=ARCHIVER_WS_PING_SECS) as ws:
                await ws.send(json.dumps({"type": "MARKET", "assets_ids": tokens}))
                backoff = 1
                async for msg in ws:
                    await _handle_book_message(msg, last_mid)
        except Exception as e:
            log.warning("CLOB book error: %s — reconnecting in %ds", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def _handle_book_message(msg: str, last_mid: dict[str, tuple[int, float]]):
    try:
        data = json.loads(msg)
    except json.JSONDecodeError:
        return
    items = data if isinstance(data, list) else [data]
    now = int(time.time())
    for it in items:
        if it.get("event_type") != "book":
            continue
        token = it.get("asset_id") or it.get("market")
        bids = it.get("bids") or []
        asks = it.get("asks") or []
        if not bids or not asks or not token:
            continue
        try:
            best_bid = max(float(b.get("price", 0)) for b in bids)
            best_ask = min(float(a.get("price", 0)) for a in asks if float(a.get("price", 0)) > 0)
        except (TypeError, ValueError):
            continue
        mid = (best_bid + best_ask) / 2.0
        last = last_mid.get(token)
        if last and (now - last[0]) < ARCHIVER_BOOK_SAMPLE_SECS:
            continue
        last_mid[token] = (now, mid)
        await _enqueue(token, now, mid, "ws_book_mid")


# ---------- entry point ----------


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stop = asyncio.Event()

    def _on_signal(*_):
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_event_loop().add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass

    tasks = [
        asyncio.create_task(_rtds_subscriber()),
        asyncio.create_task(_clob_book_subscriber()),
        asyncio.create_task(_flusher()),
        asyncio.create_task(_heartbeat()),
    ]
    done, pending = await asyncio.wait([asyncio.create_task(stop.wait()), *tasks], return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    await _flush(force=True)


if __name__ == "__main__":
    asyncio.run(main())
