"""
Cross-venue arbitrage scanner.

For each curated pair in markets.yaml, fetch top-of-book prices from
Kalshi and Polymarket US, then compute both arbitrage directions:

    Direction A: BUY YES on Kalshi + BUY NO on Polymarket US
                 cost = kalshi_yes_ask + poly_no_ask + fees
                 locked spread = $1 - cost   (per contract pair)

    Direction B: BUY YES on Polymarket US + BUY NO on Kalshi
                 cost = poly_yes_ask + kalshi_no_ask + fees
                 locked spread = $1 - cost

Only rows with locked_spread > 0 surface as opportunities.

On Polymarket, NO side prices are derived as the complement of YES
(no_ask = 1 - yes_bid, no_bid = 1 - yes_ask) — the standard binary-
market identity.
"""

from __future__ import annotations

import json
import math
import os
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from polymarket_us_client import (
    PolymarketUSClient,
    PolymarketUSError,
)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# Polymarket US taker fee in basis points. Default 10 bps = 0.10%, per
# documented "Taker Orders incur fees, Maker Orders receive a rebate"
# guidance. Exact schedule is in their rulebook; override here if it
# changes. Maker rebate is ignored in arb math — we always use IOC, so
# we always take.
POLY_US_TAKER_FEE_BPS = float(os.getenv("POLY_US_TAKER_FEE_BPS", "10"))


# ---- registry --------------------------------------------------------------

@dataclass
class PairConfig:
    key: str
    label: str
    kalshi_ticker: str
    polymarket_us_slug: str
    yes_means: str = "same"  # "same" or "inverted"
    enabled: bool = True


def load_registry(path: str | None = None) -> list[PairConfig]:
    p = Path(path or os.getenv("MARKETS_REGISTRY_PATH", "markets.yaml"))
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    pairs = raw.get("pairs") or []
    out: list[PairConfig] = []
    for entry in pairs:
        if not isinstance(entry, dict):
            continue
        try:
            cfg = PairConfig(
                key=str(entry["key"]),
                label=str(entry.get("label", entry["key"])),
                kalshi_ticker=str(entry["kalshi_ticker"]),
                polymarket_us_slug=str(entry["polymarket_us_slug"]),
                yes_means=str(entry.get("yes_means", "same")).lower(),
                enabled=bool(entry.get("enabled", True)),
            )
        except KeyError as e:
            raise ValueError(f"markets.yaml pair missing field {e}") from e
        if cfg.yes_means not in ("same", "inverted"):
            raise ValueError(
                f"markets.yaml pair {cfg.key}: yes_means must be 'same' or 'inverted'"
            )
        out.append(cfg)
    return out


# ---- fees ------------------------------------------------------------------

def kalshi_taker_fee_per_contract(yes_price: float, contracts: int) -> float:
    """Kalshi taker fee per contract.
    Formula: ceil(0.07 * N * P * (1-P) * 100) / 100  (round UP to next cent).
    """
    contracts = max(1, int(contracts))
    raw_total = 0.07 * contracts * yes_price * (1.0 - yes_price)
    rounded_total_cents = math.ceil(raw_total * 100.0)
    return (rounded_total_cents / 100.0) / contracts


def polymarket_us_taker_fee_per_contract(price: float, contracts: int = 1) -> float:
    """Polymarket US taker fee per contract.

    Fee is charged in basis points of notional (price × quantity), so
    per-contract fee = price × bps / 10_000. Quantity is informational —
    fee scales linearly so per-contract is constant for a given price.
    """
    return float(price) * POLY_US_TAKER_FEE_BPS / 10_000.0


# ---- Kalshi public market data --------------------------------------------

def fetch_kalshi_market(ticker: str) -> dict:
    """Return the raw market dict for one Kalshi ticker (single-ticker GET).

    Prefer fetch_kalshi_markets_for_event() when scanning many tickers
    from the same event — it batches in one HTTP call and avoids rate
    limits.
    """
    url = f"{KALSHI_BASE}/markets/{ticker}"
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": _BROWSER_UA}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        payload = json.loads(r.read())
    return payload.get("market") or {}


def fetch_kalshi_markets_for_event(event_ticker: str,
                                   retries: int = 3,
                                   backoff_sec: float = 1.0) -> dict[str, dict]:
    """Batch fetch: one HTTP call returns every market in an event.

    Returns `{ticker: market_dict}`. Retries on HTTP 429 with exponential
    backoff (1s, 2s, 4s by default) since Kalshi rate-limits
    unauthenticated public reads aggressively.
    """
    import urllib.parse
    url = (f"{KALSHI_BASE}/markets?"
           + urllib.parse.urlencode({"event_ticker": event_ticker, "limit": 200}))
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": _BROWSER_UA}
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                payload = json.loads(r.read())
            out: dict[str, dict] = {}
            for m in payload.get("markets", []) or []:
                t = m.get("ticker")
                if t:
                    out[t] = m
            return out
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429 and attempt < retries:
                import time as _t
                _t.sleep(backoff_sec * (2 ** attempt))
                continue
            raise
    if last_err:
        raise last_err
    return {}


def _kalshi_event_ticker_for(market_ticker: str) -> str:
    """Heuristic: Kalshi event tickers are the market ticker minus its
    final '-{outcome}' segment. KXMLB-26-DET -> KXMLB-26."""
    if "-" not in market_ticker:
        return market_ticker
    return market_ticker.rsplit("-", 1)[0]


# Module-level cache + throttle for Kalshi reads. ALL run_scan callers
# share this — /api/scan, the ntfy background loop, and /api/arb/recommend
# don't stampede when called within the TTL.
_KALSHI_CACHE_TTL_SEC = float(os.getenv("KALSHI_EVENT_CACHE_TTL_SEC", "15"))
_KALSHI_INTER_CALL_DELAY_SEC = float(os.getenv("KALSHI_INTER_CALL_DELAY_SEC", "0.25"))
_kalshi_event_cache: dict[str, tuple[float, dict[str, dict]]] = {}
import threading as _threading
_kalshi_cache_lock = _threading.Lock()


def _fetch_kalshi_events_cached(event_tickers: list[str],
                                errors: list[str]) -> dict[str, dict]:
    """Fetch a batch of event_tickers. Per-event responses are cached for
    _KALSHI_CACHE_TTL_SEC and a small delay is inserted between fresh
    fetches to stay under Kalshi's rate limit.
    """
    import time as _t
    now = _t.time()
    out: dict[str, dict] = {}
    to_fetch: list[str] = []
    with _kalshi_cache_lock:
        for ev in event_tickers:
            entry = _kalshi_event_cache.get(ev)
            if entry and now - entry[0] < _KALSHI_CACHE_TTL_SEC:
                out.update(entry[1])
            else:
                to_fetch.append(ev)

    for i, ev in enumerate(to_fetch):
        try:
            ms = fetch_kalshi_markets_for_event(ev)
            with _kalshi_cache_lock:
                _kalshi_event_cache[ev] = (_t.time(), ms)
            out.update(ms)
        except Exception as e:
            errors.append(f"kalshi event {ev}: {e}")
            # Cache the failure briefly so we don't hammer the failing event
            # for the next caller — but with a short TTL so it recovers fast.
            with _kalshi_cache_lock:
                _kalshi_event_cache[ev] = (_t.time(), {})
        if i < len(to_fetch) - 1 and _KALSHI_INTER_CALL_DELAY_SEC > 0:
            _t.sleep(_KALSHI_INTER_CALL_DELAY_SEC)
    return out


def _f(v, default: float | None = None) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_kalshi_quote(market: dict) -> dict:
    """Normalize a Kalshi market dict into a side-aware quote."""
    return {
        "ticker": market.get("ticker"),
        "yes_bid": _f(market.get("yes_bid_dollars")),
        "yes_ask": _f(market.get("yes_ask_dollars")),
        "no_bid":  _f(market.get("no_bid_dollars")),
        "no_ask":  _f(market.get("no_ask_dollars")),
        "last_price": _f(market.get("last_price_dollars")),
        "volume": _f(market.get("volume_fp"), 0.0) or 0.0,
        "status": market.get("status"),
    }


# ---- Polymarket US wrapper -------------------------------------------------

_poly_client: PolymarketUSClient | None = None


def _poly() -> PolymarketUSClient:
    global _poly_client
    if _poly_client is None:
        _poly_client = PolymarketUSClient()
    return _poly_client


def parse_poly_quote(quote: dict, *, inverted: bool) -> dict:
    """Normalize a Polymarket US quote dict (from get_quote()) into a
    YES-and-NO quote, applying the registry's yes_means flag."""
    bid = quote.get("yes_bid")
    ask = quote.get("yes_ask")
    if bid is None or ask is None:
        return {
            "slug": quote.get("slug"),
            "yes_bid": None, "yes_ask": None,
            "no_bid": None, "no_ask": None,
        }
    if inverted:
        # The Polymarket market's YES is actually our pair's NO. Flip.
        yes_bid, yes_ask = 1.0 - ask, 1.0 - bid
    else:
        yes_bid, yes_ask = bid, ask
    return {
        "slug": quote.get("slug"),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        # Binary-market identity:
        "no_bid": 1.0 - yes_ask,
        "no_ask": 1.0 - yes_bid,
    }


# ---- arb math --------------------------------------------------------------

@dataclass
class Leg:
    venue: str       # "kalshi" or "polymarket_us"
    market_id: str   # ticker or slug
    side: str        # "yes" or "no"
    price: float     # ask we'd pay (probability 0-1)
    fee: float       # per-contract dollar fee
    bid: float | None = None  # for context only

    def as_dict(self) -> dict:
        return {
            "venue": self.venue,
            "market_id": self.market_id,
            "side": self.side,
            "price": self.price,
            "price_cents": round(self.price * 100, 2),
            "fee": self.fee,
            "fee_cents": round(self.fee * 100, 2),
            "bid": self.bid,
        }


def _build_legs(kq: dict, pq: dict, contracts: int) -> list[dict]:
    """Build candidate arb rows for one pair from its Kalshi + Poly quotes."""
    rows: list[dict] = []

    # Direction A: BUY YES on Kalshi + BUY NO on Polymarket
    if kq.get("yes_ask") is not None and pq.get("no_ask") is not None:
        ka = kq["yes_ask"]; pa = pq["no_ask"]
        if 0 < ka < 1 and 0 < pa < 1:
            kfee = kalshi_taker_fee_per_contract(ka, contracts)
            pfee = polymarket_us_taker_fee_per_contract(pa, contracts)
            cost = ka + pa + kfee + pfee
            rows.append({
                "direction": "A",
                "direction_label": "Kalshi YES + Polymarket NO",
                "kalshi_leg": Leg("kalshi", kq["ticker"], "yes",
                                  ka, kfee, bid=kq.get("yes_bid")).as_dict(),
                "poly_leg": Leg("polymarket_us", pq["slug"], "no",
                                pa, pfee, bid=pq.get("no_bid")).as_dict(),
                "cost_per_contract": cost,
                "locked_spread": 1.0 - cost,
                "locked_spread_cents": round((1.0 - cost) * 100, 2),
                "fees_total_cents": round((kfee + pfee) * 100, 2),
            })

    # Direction B: BUY YES on Polymarket + BUY NO on Kalshi
    if pq.get("yes_ask") is not None and kq.get("no_ask") is not None:
        pa = pq["yes_ask"]; ka = kq["no_ask"]
        if 0 < pa < 1 and 0 < ka < 1:
            # Kalshi fees treat YES/NO symmetrically (P*(1-P) is the same
            # function), so feeding the NO ask in is correct.
            kfee = kalshi_taker_fee_per_contract(ka, contracts)
            pfee = polymarket_us_taker_fee_per_contract(pa, contracts)
            cost = pa + ka + kfee + pfee
            rows.append({
                "direction": "B",
                "direction_label": "Polymarket YES + Kalshi NO",
                "kalshi_leg": Leg("kalshi", kq["ticker"], "no",
                                  ka, kfee, bid=kq.get("no_bid")).as_dict(),
                "poly_leg": Leg("polymarket_us", pq["slug"], "yes",
                                pa, pfee, bid=pq.get("yes_bid")).as_dict(),
                "cost_per_contract": cost,
                "locked_spread": 1.0 - cost,
                "locked_spread_cents": round((1.0 - cost) * 100, 2),
                "fees_total_cents": round((kfee + pfee) * 100, 2),
            })

    return rows


# ---- entry point -----------------------------------------------------------

def run_scan(contracts: int = 100, min_spread_cents: float = 0.0,
             registry_path: str | None = None) -> dict:
    """Scan every enabled pair, return both arb directions.

    contracts:        size used to compute fee per contract (Kalshi fee
                      depends on contract count due to round-up-to-cent).
    min_spread_cents: only return rows with locked_spread >= this many cents
                      per contract. Default 0 = show every positive arb.
                      Pass negative to surface near-misses for debugging.
    """
    pairs = load_registry(registry_path)
    poly = _poly()

    all_rows: list[dict] = []
    pairs_info: list[dict] = []
    errors: list[str] = []

    # --- Batch Kalshi reads: group pairs by event_ticker so we issue ONE
    #     /markets?event_ticker=X call per event instead of one per pair.
    by_event: dict[str, list] = {}
    for cfg in pairs:
        if not cfg.enabled:
            continue
        by_event.setdefault(_kalshi_event_ticker_for(cfg.kalshi_ticker), []).append(cfg)

    kalshi_market_by_ticker: dict[str, dict] = _fetch_kalshi_events_cached(
        list(by_event.keys()), errors,
    )

    for cfg in pairs:
        if not cfg.enabled:
            continue
        kalshi_q: dict = {}
        poly_q: dict = {}
        km = kalshi_market_by_ticker.get(cfg.kalshi_ticker)
        if km:
            kalshi_q = parse_kalshi_quote(km)
        elif not any(e.startswith(f"kalshi event {_kalshi_event_ticker_for(cfg.kalshi_ticker)}:")
                     for e in errors):
            # Event call succeeded but this ticker wasn't in the response
            # (settled / not yet open). Note it once.
            errors.append(f"kalshi {cfg.key} ({cfg.kalshi_ticker}): not in event response")
        try:
            poly_raw = poly.get_quote(cfg.polymarket_us_slug)
            poly_q = parse_poly_quote(poly_raw, inverted=(cfg.yes_means == "inverted"))
        except PolymarketUSError as e:
            errors.append(f"polymarket {cfg.key} ({cfg.polymarket_us_slug}): {e}")
        rows = _build_legs(kalshi_q, poly_q, contracts) if (kalshi_q and poly_q) else []
        for r in rows:
            r["pair_key"] = cfg.key
            r["label"] = cfg.label
        kept = [r for r in rows if r["locked_spread_cents"] >= min_spread_cents]
        all_rows.extend(kept)
        pairs_info.append({
            "key": cfg.key,
            "label": cfg.label,
            "kalshi_ticker": cfg.kalshi_ticker,
            "polymarket_us_slug": cfg.polymarket_us_slug,
            "yes_means": cfg.yes_means,
            "kalshi_ok": bool(kalshi_q),
            "poly_ok": bool(poly_q),
            "row_count": len(kept),
            "kalshi_quote": kalshi_q,
            "poly_quote": poly_q,
        })

    all_rows.sort(key=lambda r: r["locked_spread_cents"], reverse=True)
    return {
        "rows": all_rows,
        "pairs": pairs_info,
        "contracts": contracts,
        "min_spread_cents": min_spread_cents,
        "poly_us_taker_fee_bps": POLY_US_TAKER_FEE_BPS,
        "errors": errors,
    }
