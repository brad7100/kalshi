"""
Shared scanner logic: pull Kalshi + Polymarket Eurovision prices and
compute EV. Used by both the CLI (eurovision_ev.py) and the FastAPI
backend (main.py).
"""

from __future__ import annotations

import json
import math
import urllib.request
from typing import Any

from polymarket_client import (
    PolymarketError,
    fetch_event,
    parse_country_markets,
)

KALSHI_EVENT_URL = (
    "https://api.elections.kalshi.com/trade-api/v2/markets"
    "?event_ticker=KXEUROVISION-26&limit=200&status=open"
)

# Kalshi ticker suffix -> internal country name
TICKER_TO_COUNTRY = {
    "ALB": "Albania", "ARM": "Armenia", "AUS": "Australia", "AUST": "Austria",
    "AZE": "Azerbaijan", "BEL": "Belgium", "BUL": "Bulgaria", "CRO": "Croatia",
    "CYR": "Cyprus", "CZE": "Czechia", "DEN": "Denmark", "EST": "Estonia",
    "FIN": "Finland", "FRA": "France", "GEO": "Georgia", "GER": "Germany",
    "GRE": "Greece", "ISR": "Israel", "ITA": "Italy", "LAT": "Latvia",
    "LIT": "Lithuania", "LUX": "Luxembourg", "MAL": "Malta", "MOL": "Moldova",
    "MON": "Montenegro", "NOR": "Norway", "POL": "Poland", "POR": "Portugal",
    "ROM": "Romania", "SAN": "San Marino", "SER": "Serbia", "SWE": "Sweden",
    "SWI": "Switzerland", "UKR": "Ukraine", "UNI": "United Kingdom",
}
COUNTRY_TO_TICKER = {v: k for k, v in TICKER_TO_COUNTRY.items()}

POLYMARKET_ALIASES = {
    "Czech Republic": "Czechia",
    "UK": "United Kingdom",
    "Great Britain": "United Kingdom",
}


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def normalize_country(name: str) -> str:
    name = name.strip()
    return POLYMARKET_ALIASES.get(name, name)


# ---- Kalshi public market data --------------------------------------------

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def fetch_kalshi_public() -> dict:
    req = urllib.request.Request(
        KALSHI_EVENT_URL,
        headers={"Accept": "application/json", "User-Agent": _BROWSER_UA},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def parse_kalshi(payload: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for m in payload.get("markets", []):
        ticker = m.get("ticker", "")
        suffix = ticker.rsplit("-", 1)[-1]
        country = TICKER_TO_COUNTRY.get(suffix)
        if not country:
            continue
        out[country] = {
            "ticker": ticker,
            "yes_ask": _f(m.get("yes_ask_dollars")),
            "yes_bid": _f(m.get("yes_bid_dollars")),
            "last_price": _f(m.get("last_price_dollars")),
            "volume": _f(m.get("volume_fp")),
        }
    return out


# ---- Polymarket -----------------------------------------------------------

def fetch_polymarket() -> tuple[dict[str, dict], dict[str, Any]]:
    event = fetch_event()
    raw = parse_country_markets(event)
    poly = {normalize_country(k): v for k, v in raw.items()}
    return poly, event


# ---- EV math --------------------------------------------------------------

def kalshi_taker_fee_per_contract(yes_price: float, contracts: int) -> float:
    """Kalshi taker fee per contract.
    Formula: ceil(0.07 * N * P * (1-P) * 100) / 100  (round UP to next cent).
    """
    contracts = max(1, int(contracts))
    raw_total = 0.07 * contracts * yes_price * (1.0 - yes_price)
    rounded_total_cents = math.ceil(raw_total * 100.0)
    return (rounded_total_cents / 100.0) / contracts


def pick_true_prob(poly_row: dict, side: str) -> float | None:
    if side == "bid":
        return poly_row.get("yes_bid")
    if side == "ask":
        return poly_row.get("yes_ask")
    if side == "last":
        return poly_row.get("yes_last")
    return poly_row.get("yes_mid") or poly_row.get("yes_ask")


def compute_rows(kalshi: dict, poly: dict, contracts: int,
                 price_side: str = "mid") -> list[dict]:
    rows = []
    for country, p in poly.items():
        k = kalshi.get(country)
        if not k:
            continue
        yes_ask = k["yes_ask"]
        if yes_ask <= 0 or yes_ask >= 1:
            continue
        true_prob = pick_true_prob(p, price_side)
        if true_prob is None or true_prob <= 0:
            continue
        fee = kalshi_taker_fee_per_contract(yes_ask, contracts)
        ev_per_contract = (
            true_prob * (1.0 - yes_ask)
            - (1.0 - true_prob) * yes_ask
            - fee
        )
        rows.append({
            "country": country,
            "ticker": k["ticker"],
            "kalshi_bid": k["yes_bid"],
            "kalshi_ask": yes_ask,
            "kalshi_volume": k["volume"],
            "poly_bid": p.get("yes_bid"),
            "poly_ask": p.get("yes_ask"),
            "poly_mid": p.get("yes_mid"),
            "poly_last": p.get("yes_last"),
            "poly_volume": p.get("volume", 0),
            "true_prob": true_prob,
            "edge_pp": (true_prob - yes_ask) * 100,
            "ev_per_contract": ev_per_contract,
            "ev_per_dollar": ev_per_contract / yes_ask if yes_ask else 0.0,
            "fee_per_contract": fee,
        })
    rows.sort(key=lambda r: r["ev_per_dollar"], reverse=True)
    return rows


def run_scan(contracts: int = 100, price_side: str = "mid") -> dict:
    """One-shot scan. Returns a JSON-serializable dict."""
    errors = []
    try:
        kalshi = parse_kalshi(fetch_kalshi_public())
    except Exception as e:
        kalshi = {}
        errors.append(f"kalshi: {e}")
    try:
        poly, event = fetch_polymarket()
        event_title = event.get("title", "")
    except PolymarketError as e:
        poly, event_title = {}, ""
        errors.append(f"polymarket: {e}")
    rows = compute_rows(kalshi, poly, contracts, price_side)
    return {
        "rows": rows,
        "kalshi_count": len(kalshi),
        "poly_count": len(poly),
        "poly_event_title": event_title,
        "contracts": contracts,
        "price_side": price_side,
        "errors": errors,
    }
