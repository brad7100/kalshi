"""
Shared scanner logic: pull Kalshi + Polymarket Eurovision prices for the
Winner, Jury, and Televote events, then compute EV. Used by both the CLI
(eurovision_ev.py) and the FastAPI backend (main.py).
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

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2/markets"

# Each event we support. Adding more (Top N, Last Place, semifinals) is just
# a matter of finding the Kalshi event_ticker + Polymarket slug pair.
EVENTS: list[dict[str, str]] = [
    {
        "key": "winner",
        "label": "Winner",
        "kalshi_event": "KXEUROVISION-26",
        "polymarket_slug": "eurovision-winner-2026",
    },
    {
        "key": "jury",
        "label": "Jury",
        "kalshi_event": "KXEUROVISIONJURY-26",
        "polymarket_slug": "eurovision-2026-jury-winner",
    },
    {
        "key": "televote",
        "label": "Televote",
        "kalshi_event": "KXEUROVISIONTELEV-26",
        "polymarket_slug": "eurovision-2026-televote-winner",
    },
]

# Kalshi ticker suffix -> internal country name. The televote event uses
# CYP for Cyprus while the winner event uses CYR — both map to the same
# country.
TICKER_TO_COUNTRY = {
    "ALB": "Albania", "ARM": "Armenia", "AUS": "Australia", "AUST": "Austria",
    "AZE": "Azerbaijan", "BEL": "Belgium", "BUL": "Bulgaria", "CRO": "Croatia",
    "CYR": "Cyprus", "CYP": "Cyprus",
    "CZE": "Czechia", "DEN": "Denmark", "EST": "Estonia",
    "FIN": "Finland", "FRA": "France", "GEO": "Georgia", "GER": "Germany",
    "GRE": "Greece", "ISR": "Israel", "ITA": "Italy", "LAT": "Latvia",
    "LIT": "Lithuania", "LUX": "Luxembourg", "MAL": "Malta", "MOL": "Moldova",
    "MON": "Montenegro", "NOR": "Norway", "POL": "Poland", "POR": "Portugal",
    "ROM": "Romania", "SAN": "San Marino", "SER": "Serbia", "SWE": "Sweden",
    "SWI": "Switzerland", "UKR": "Ukraine", "UNI": "United Kingdom",
}

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


def fetch_kalshi_event(event_ticker: str) -> dict:
    url = f"{KALSHI_BASE}?event_ticker={event_ticker}&limit=200&status=open"
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": _BROWSER_UA}
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
            "no_ask":  _f(m.get("no_ask_dollars")),
            "no_bid":  _f(m.get("no_bid_dollars")),
            "last_price": _f(m.get("last_price_dollars")),
            "volume": _f(m.get("volume_fp")),
        }
    return out


# ---- Polymarket -----------------------------------------------------------

def fetch_polymarket(slug: str) -> tuple[dict[str, dict], dict[str, Any]]:
    event = fetch_event(slug)
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
    """Produce one row per (country, side). 'side' is "yes" or "no".

    On a Kalshi binary market, there are two distinct buy opportunities:
        BUY YES at yes_ask  — pays $1 if the event happens
        BUY NO  at no_ask   — pays $1 if it doesn't
    These have independent prices and frequently asymmetric EV. The +EV
    edge often shows up on the NO side of heavy favorites (e.g. fading
    Finland) rather than the YES side. We emit a row for each tradable
    side so the UI can show both opportunities.
    """
    rows: list[dict] = []
    for country, p in poly.items():
        k = kalshi.get(country)
        if not k:
            continue
        true_prob_yes = pick_true_prob(p, price_side)
        if true_prob_yes is None or true_prob_yes <= 0:
            continue
        common = {
            "country": country,
            "ticker": k["ticker"],
            "kalshi_volume": k["volume"],
            "poly_bid": p.get("yes_bid"),
            "poly_ask": p.get("yes_ask"),
            "poly_mid": p.get("yes_mid"),
            "poly_last": p.get("yes_last"),
            "poly_volume": p.get("volume", 0),
            "poly_true_prob_yes": true_prob_yes,
        }
        for side, ask, bid, true_prob in (
            ("yes", k.get("yes_ask"), k.get("yes_bid"), true_prob_yes),
            ("no",  k.get("no_ask"),  k.get("no_bid"),  1.0 - true_prob_yes),
        ):
            if ask is None or ask <= 0 or ask >= 1:
                continue
            fee = kalshi_taker_fee_per_contract(ask, contracts)
            ev_per_contract = (
                true_prob * (1.0 - ask)
                - (1.0 - true_prob) * ask
                - fee
            )
            rows.append({**common,
                "side": side,
                "kalshi_bid": bid,
                "kalshi_ask": ask,
                "true_prob": true_prob,
                "edge_pp": (true_prob - ask) * 100,
                "ev_per_contract": ev_per_contract,
                "ev_per_dollar": ev_per_contract / ask if ask else 0.0,
                "fee_per_contract": fee,
            })
    return rows


def run_scan(contracts: int = 100, price_side: str = "mid") -> dict:
    """Scan every configured event and return a unified row set. Each row
    carries event_key/event_label so the UI can group or label by event.
    Errors per event are isolated — a failure on one doesn't kill the rest.
    """
    all_rows: list[dict] = []
    events_info: list[dict] = []
    errors: list[str] = []
    for ev in EVENTS:
        try:
            kalshi = parse_kalshi(fetch_kalshi_event(ev["kalshi_event"]))
        except Exception as e:
            errors.append(f"kalshi {ev['key']}: {e}")
            kalshi = {}
        try:
            poly, poly_event = fetch_polymarket(ev["polymarket_slug"])
        except PolymarketError as e:
            errors.append(f"polymarket {ev['key']}: {e}")
            poly, poly_event = {}, {}
        rows = compute_rows(kalshi, poly, contracts, price_side)
        for r in rows:
            r["event_key"] = ev["key"]
            r["event_label"] = ev["label"]
        all_rows.extend(rows)
        events_info.append({
            "key": ev["key"],
            "label": ev["label"],
            "kalshi_event_ticker": ev["kalshi_event"],
            "polymarket_slug": ev["polymarket_slug"],
            "kalshi_count": len(kalshi),
            "poly_count": len(poly),
            "row_count": len(rows),
            "poly_event_title": poly_event.get("title", "") if poly_event else "",
        })
    all_rows.sort(key=lambda r: r["ev_per_dollar"], reverse=True)
    return {
        "rows": all_rows,
        "events": events_info,
        "contracts": contracts,
        "price_side": price_side,
        "errors": errors,
    }
