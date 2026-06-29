"""Workaround for local DNS poisoning of *.polymarket.com.

Some networks (notably mainland China) return forged IPs for polymarket hostnames.
We resolve via Cloudflare DoH instead, cache the result for 5 minutes, and tell
httpx to dial that IP while preserving the Host/SNI for cert validation.
"""
from __future__ import annotations

import logging
import socket
import threading
import time

import httpx

log = logging.getLogger(__name__)

POISONED_DOMAINS = (
    "polymarket.com",  # matches *.polymarket.com via suffix check
    "goldsky.com",
)
DOH_URL = "https://1.1.1.1/dns-query"
CACHE_TTL_SEC = 300

_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


def _is_poisoned(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in POISONED_DOMAINS)


def _doh_resolve(host: str) -> str | None:
    try:
        r = httpx.get(
            DOH_URL,
            params={"name": host, "type": "A"},
            headers={"Accept": "application/dns-json"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        answers = [a for a in data.get("Answer") or [] if a.get("type") == 1]
        if answers:
            return answers[0]["data"]
    except Exception as e:
        log.warning("DoH lookup for %s failed: %s", host, e)
    return None


def resolve(host: str) -> str | None:
    """Return a clean IP for `host` if it's in the poisoned list. Cached."""
    if not _is_poisoned(host):
        return None
    with _lock:
        cached = _cache.get(host)
        now = time.time()
        if cached and (now - cached[0]) < CACHE_TTL_SEC:
            return cached[1]
    ip = _doh_resolve(host)
    if ip:
        with _lock:
            _cache[host] = (time.time(), ip)
        log.info("DoH %s → %s", host, ip)
    return ip


class CleanDnsTransport(httpx.HTTPTransport):
    """httpx transport that swaps the destination IP for poisoned hostnames.

    Keeps the URL host (so SNI + cert verification still target the real domain),
    just rewrites the resolved address that httpcore uses to open the socket.
    """

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        ip = resolve(host)
        if ip:
            # httpx/httpcore picks up `extensions['socket_options']` not enough;
            # easier: monkey-patch socket.getaddrinfo for this thread/call.
            with _patched_getaddrinfo(host, ip):
                return super().handle_request(request)
        return super().handle_request(request)


from contextlib import contextmanager


@contextmanager
def _patched_getaddrinfo(host: str, ip: str):
    original = socket.getaddrinfo

    def patched(h, port, *args, **kwargs):
        if h == host:
            return original(ip, port, *args, **kwargs)
        return original(h, port, *args, **kwargs)

    socket.getaddrinfo = patched
    try:
        yield
    finally:
        socket.getaddrinfo = original
