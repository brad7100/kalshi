"""
Read-only client for Polymarket international (polymarket.com) via the
Gamma REST API. NOT the CFTC-regulated US site — different host, different
auth model, different product set.

Why we need this:
  - Polymarket US (api.polymarket.us) doesn't list daily moneyline games
    — it's futures-only as of mid-2026.
  - Polymarket international (polymarket.com / gamma-api.polymarket.com)
    has thousands of daily MLB/NBA/NHL/NFL game markets.
  - Matching Kalshi daily games against the international Polymarket lets
    the scanner identify arbs that don't exist on the US-only data.

What this client does NOT do:
  - Trade execution. International Polymarket runs on Polygon (USDC,
    EIP-712 signed orders, on-chain settlement). Adding trade execution
    requires a Polygon wallet, USDC funding, and the py-clob-client
    library — out of scope for read-only arb identification.

Trade execution for any opportunity surfaced from an intl market is
manual on the user's part. The scanner flags it; you trade it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

GAMMA_BASE = "https://gamma-api.polymarket.com"

_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}


class PolymarketIntlError(RuntimeError):
    pass


def _f(v, default: float | None = None) -> float | None:
    try:
        return float(v) if v is not None and v != "" else default
    except (TypeError, ValueError):
        return default


def _gget(path: str, params: dict | None = None) -> Any:
    url = GAMMA_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise PolymarketIntlError(f"GET {url} HTTP {e.code}: {e.read()!r}") from e
    except urllib.error.URLError as e:
        raise PolymarketIntlError(f"GET {url} network error: {e}") from e


def get_market_by_slug(slug: str) -> dict | None:
    """Fetch one Gamma market by slug. Returns None if not found."""
    res = _gget("/markets", {"slug": slug})
    items = res if isinstance(res, list) else (res or {}).get("markets") or []
    return items[0] if items else None


def get_quote(slug: str) -> dict:
    """Normalized quote for one Polymarket intl market.

    Gamma binary markets report `bestBid` / `bestAsk` from the perspective
    of `outcomes[0]`. For arb scanning we treat outcomes[0] as YES on the
    pair (registry decides which Kalshi side that corresponds to).

    Returns:
      {
        "slug": str,
        "yes_bid": float|None,   # bid on outcomes[0]
        "yes_ask": float|None,   # ask on outcomes[0]
        "no_bid": float|None,    # = 1 - yes_ask
        "no_ask": float|None,    # = 1 - yes_bid
        "outcomes": list[str],
        "end_date": str|None,
        "volume": float,
      }
    """
    m = get_market_by_slug(slug)
    if not m:
        return {"slug": slug, "yes_bid": None, "yes_ask": None,
                "no_bid": None, "no_ask": None,
                "outcomes": [], "end_date": None, "volume": 0.0}
    bid = _f(m.get("bestBid"))
    ask = _f(m.get("bestAsk"))
    outcomes_raw = m.get("outcomes")
    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else (outcomes_raw or [])
    except (json.JSONDecodeError, TypeError):
        outcomes = []
    out: dict = {
        "slug": m.get("slug", slug),
        "yes_bid": bid,
        "yes_ask": ask,
        "no_bid": (1.0 - ask) if ask is not None else None,
        "no_ask": (1.0 - bid) if bid is not None else None,
        "outcomes": outcomes,
        "end_date": m.get("endDate"),
        "volume": _f(m.get("volume"), 0.0) or 0.0,
        "condition_id": m.get("conditionId"),
        "active": m.get("active"),
        "closed": m.get("closed"),
    }
    return out


def list_events_by_tag(tag_slug: str, limit: int = 200,
                       closed: bool = False) -> list[dict]:
    """List Gamma events for a tag (e.g. 'baseball', 'basketball').
    Returns the event list."""
    res = _gget("/events", {
        "tag_slug": tag_slug,
        "limit": limit,
        "closed": "true" if closed else "false",
    })
    return res if isinstance(res, list) else (res or {}).get("events") or []


def list_markets_filter(*, closed: bool = False, limit: int = 500,
                        active: bool | None = None,
                        category: str | None = None,
                        slug_contains: str | None = None) -> list[dict]:
    """Pull open markets matching simple filters. Useful for discovering
    daily game markets via slug-pattern (e.g. slug starts with 'mlb-')."""
    params: dict = {"limit": limit, "closed": "true" if closed else "false"}
    if active is not None:
        params["active"] = "true" if active else "false"
    if category:
        params["category"] = category
    res = _gget("/markets", params)
    markets = res if isinstance(res, list) else (res or {}).get("markets") or []
    if slug_contains:
        markets = [m for m in markets if slug_contains in (m.get("slug") or "")]
    return markets
