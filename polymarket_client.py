"""
Polymarket Gamma API client (read-only, no auth required).

The Eurovision Winner 2026 event is a grouped event of ~35 binary
"Will <Country> win Eurovision 2026?" markets, one per country.

Docs: https://docs.polymarket.com/  (Gamma is the public REST API for
catalog/discovery; the on-chain CLOB has its own endpoint for trading.)
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

GAMMA_BASE = "https://gamma-api.polymarket.com"
EUROVISION_EVENT_SLUG = "eurovision-winner-2026"

# "Will <Country> win Eurovision 2026?" -> "<Country>"
_QUESTION_RE = re.compile(r"^Will\s+(.+?)\s+win Eurovision 2026\??$", re.IGNORECASE)


class PolymarketError(RuntimeError):
    pass


# Polymarket's Gamma API sits behind Cloudflare and 403s requests with the
# default Python user-agent. Send a normal browser UA.
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}


def _get(url: str) -> Any:
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise PolymarketError(f"GET {url} HTTP {e.code}: {e.read()!r}") from e


def _f(v, default: float | None = None) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def fetch_event(slug: str = EUROVISION_EVENT_SLUG) -> dict:
    """Fetch the Eurovision Winner event with all nested per-country markets."""
    payload = _get(f"{GAMMA_BASE}/events?slug={slug}")
    if not payload:
        raise PolymarketError(f"No event found for slug={slug}")
    if isinstance(payload, list):
        payload = payload[0]
    return payload


def parse_country_markets(event: dict) -> dict[str, dict]:
    """Return {country_name: {"yes_bid", "yes_ask", "yes_mid", "yes_last",
    "volume", "condition_id", "slug"}}. Markets with no question match,
    empty prices, or placeholder names ("Country C", etc.) are skipped."""
    out: dict[str, dict] = {}
    for m in event.get("markets", []):
        q = m.get("question", "")
        match = _QUESTION_RE.match(q)
        if not match:
            continue
        country = match.group(1).strip()
        # Skip placeholder rows like "Country C", "another country", etc.
        if re.match(r"^Country [A-Z]$", country):
            continue
        if not country[0].isupper():
            continue
        bid = _f(m.get("bestBid"))
        ask = _f(m.get("bestAsk"))
        last = _f(m.get("lastTradePrice"))
        if bid is None and ask is None:
            continue
        mid = None
        if bid is not None and ask is not None and 0 < bid <= ask < 1:
            mid = (bid + ask) / 2.0
        out[country] = {
            "yes_bid": bid,
            "yes_ask": ask,
            "yes_mid": mid,
            "yes_last": last,
            "volume": _f(m.get("volume"), 0.0) or 0.0,
            "condition_id": m.get("conditionId"),
            "slug": m.get("slug"),
        }
    return out
