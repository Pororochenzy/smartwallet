from __future__ import annotations

from typing import Any, Iterable, Iterator

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import CLOB_API, DATA_API, GAMMA_API, SUBGRAPH_URL
from .dns_fix import CleanDnsTransport

DEFAULT_TIMEOUT = 30.0
_RETRYABLE = (httpx.HTTPStatusError, httpx.TransportError)


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=DEFAULT_TIMEOUT,
        headers={"User-Agent": "smartmoney/0.1 (+local)"},
        transport=CleanDnsTransport(retries=1),
    )


def _should_retry(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(exc, httpx.TransportError)


@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    retry=retry_if_exception_type(_RETRYABLE),
)
def _get_json(client: httpx.Client, url: str, params: dict | None = None) -> Any:
    r = client.get(url, params=params)
    if r.status_code >= 400:
        r.raise_for_status()
    return r.json()


@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    retry=retry_if_exception_type(_RETRYABLE),
)
def _post_json(client: httpx.Client, url: str, json: dict) -> Any:
    r = client.post(url, json=json)
    if r.status_code >= 400:
        r.raise_for_status()
    return r.json()


# ---------- Data API ----------


def fetch_leaderboard(period: str, limit: int = 500) -> list[dict]:
    """period: '1d' | '7d' | '30d' | 'all'. Returns list of {address, pnl, volume, ...}."""
    with _client() as c:
        data = _get_json(
            c,
            f"{DATA_API}/leaderboard",
            params={"period": period, "limit": limit},
        )
    if isinstance(data, dict) and "results" in data:
        return data["results"]
    return data or []


def iter_user_trades(user: str, since_ts: int | None = None) -> Iterator[dict]:
    """Yield every trade for a user, paginating cursors. since_ts filters server-side if supported."""
    with _client() as c:
        offset = 0
        page_size = 500
        while True:
            params: dict[str, Any] = {
                "user": user,
                "limit": page_size,
                "offset": offset,
            }
            if since_ts:
                params["from"] = since_ts
            page = _get_json(c, f"{DATA_API}/trades", params=params)
            if not page:
                break
            rows = page if isinstance(page, list) else page.get("data", [])
            if not rows:
                break
            for row in rows:
                yield row
            if len(rows) < page_size:
                break
            offset += page_size


def fetch_user_positions(user: str) -> list[dict]:
    with _client() as c:
        data = _get_json(c, f"{DATA_API}/positions", params={"user": user})
    return data if isinstance(data, list) else data.get("data", [])


# ---------- Gamma API ----------


def fetch_markets_by_condition_ids(condition_ids: Iterable[str]) -> list[dict]:
    ids = list({c for c in condition_ids if c})
    if not ids:
        return []
    out: list[dict] = []
    with _client() as c:
        for chunk_start in range(0, len(ids), 100):
            chunk = ids[chunk_start : chunk_start + 100]
            data = _get_json(
                c,
                f"{GAMMA_API}/markets",
                params=[("condition_ids", cid) for cid in chunk],
            )
            if isinstance(data, list):
                out.extend(data)
            elif isinstance(data, dict) and "data" in data:
                out.extend(data["data"])
    return out


# ---------- CLOB API ----------


def fetch_clob_price_history(
    token_id: str, start_ts: int, end_ts: int, fidelity: int = 60
) -> list[dict]:
    """Returns [{t, p}] points. fidelity in minutes. 12hr is the practical lower bound for old markets."""
    with _client() as c:
        data = _get_json(
            c,
            f"{CLOB_API}/prices-history",
            params={
                "market": token_id,
                "startTs": start_ts,
                "endTs": end_ts,
                "fidelity": fidelity,
            },
        )
    if isinstance(data, dict):
        return data.get("history", [])
    return data or []


def fetch_clob_midpoint(token_ids: Iterable[str]) -> dict[str, float]:
    ids = list(token_ids)
    if not ids:
        return {}
    with _client() as c:
        data = _get_json(
            c,
            f"{CLOB_API}/midpoints",
            params=[("token_ids", t) for t in ids],
        )
    return {k: float(v) for k, v in (data or {}).items()}


# ---------- Subgraph (fallback for trade gaps) ----------

_SUBGRAPH_TRADES_QUERY = """
query Trades($user: String!, $since: BigInt!, $first: Int!) {
  orderFilledEvents(
    where: { or: [{ maker: $user }, { taker: $user }], timestamp_gt: $since }
    orderBy: timestamp
    orderDirection: asc
    first: $first
  ) {
    id
    maker
    taker
    makerAssetId
    takerAssetId
    makerAmountFilled
    takerAmountFilled
    timestamp
    transactionHash
  }
}
"""


def fetch_subgraph_user_trades(user: str, since_ts: int, limit: int = 1000) -> list[dict]:
    with _client() as c:
        data = _post_json(
            c,
            SUBGRAPH_URL,
            json={
                "query": _SUBGRAPH_TRADES_QUERY,
                "variables": {
                    "user": user.lower(),
                    "since": str(since_ts),
                    "first": limit,
                },
            },
        )
    return (data.get("data") or {}).get("orderFilledEvents") or []
