"""
Deterministic, rule-based pair matching for sports series — no LLM
needed.

Both venues organize championship/MVP markets as one market per team
or per player. The ticker/slug pattern is consistent within a series:

    Kalshi   KXNHL-26-EDM           Edmonton Oilers wins Stanley Cup
    Poly     tec-nhl-scw-...-edm    Edmonton Oilers wins Stanley Cup

For team-based series, outcome codes match closely between venues
(BOS, EDM, GSW...) with a small alias table for cases where they
differ (Kalshi MTL ↔ Polymarket mon, Kalshi VGK ↔ Polymarket veg).

For player-based series the codes diverge (Kalshi: AMAT, Polymarket:
nathan-macKinnon), so we match by normalizing the human-readable
name (`yes_sub_title` on Kalshi, `question` on Polymarket).

Output: a list of pair dicts ready to append to markets.yaml. Each
match is high-confidence by construction (same series + same
outcome code/name), so no LLM verification needed.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from polymarket_us_client import PolymarketUSClient, PolymarketUSError

log = logging.getLogger("rule_match")

_KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
_UA = "Mozilla/5.0 (compatible; ArbScanner-RuleMatch)"


# ---- registry of sports series we match -----------------------------------
#
# Add a new series by appending one entry. The matcher then takes care of
# fetching outcomes from both venues and producing pair dicts.

@dataclass
class SeriesRule:
    key: str                # short id, unique
    label: str              # human-readable, used as registry pair label prefix
    kalshi_event_ticker: str
    poly_slug_prefix: str   # everything BEFORE the outcome code in a Poly slug
    match_kind: str         # "team" or "player"
    yes_means: str = "same"
    # Optional outcome-code aliases for the team case. Kalshi/Polymarket
    # mostly agree on team codes; this table fills the gaps.
    kalshi_to_poly_alias: dict[str, str] = None  # type: ignore[assignment]
    # Per-series outcome filters — useful when one venue uses extra entries
    # (e.g. KXWNBAMVP-26-TIE for "Tie/Co-Winners") that don't pair across.
    skip_kalshi_outcomes: set[str] = None  # type: ignore[assignment]


SERIES: list[SeriesRule] = [
    # --- Team-based: outcome codes match directly (with small aliases) ---
    SeriesRule(
        key="nhl_stanley_cup_2026",
        label="NHL Stanley Cup 2026",
        kalshi_event_ticker="KXNHL-26",
        poly_slug_prefix="tec-nhl-scw",
        match_kind="team",
        kalshi_to_poly_alias={
            "MTL": "mon",   # Montreal Canadiens
            "VGK": "veg",   # Vegas Golden Knights
            "TBL": "tb",    # Tampa Bay Lightning
            "NJD": "nj",    # New Jersey Devils
            "NYI": "nyi",   # NY Islanders (Kalshi sometimes uses NYI)
            "NYR": "nyr",   # NY Rangers
            "SJS": "sj",    # San Jose Sharks
            "LAK": "la",    # LA Kings
        },
    ),
    SeriesRule(
        key="nba_champion_2026",
        label="2026 NBA Champion",
        kalshi_event_ticker="KXNBA-26",
        poly_slug_prefix="tec-nba-champ",
        match_kind="team",
        kalshi_to_poly_alias={
            "NYK": "ny",    # NY Knicks
            "NOP": "no",    # New Orleans Pelicans
            "LAL": "lal",   # Lakers
            "LAC": "lac",   # Clippers
            "SAS": "sa",    # San Antonio Spurs
            "GSW": "gsw",   # Golden State
        },
    ),
    SeriesRule(
        key="mlb_world_series_2026",
        label="2026 MLB World Series",
        kalshi_event_ticker="KXMLB-26",
        poly_slug_prefix="tec-mlb-champ",
        match_kind="team",
        kalshi_to_poly_alias={
            # Kalshi tends to use the team's "official" 3-letter abbrev
            # (NYM, NYY, ATH); Polymarket uses 2-3 letter slugs in lowercase.
            # Many match 1:1 in lowercase; aliases here only for divergences.
            "NYY": "nyy",
            "NYM": "nym",
            "ATH": "ath",
            "AZ":  "az",
            "WSH": "wsh",
        },
    ),
    SeriesRule(
        key="mlb_al_champion_2026",
        label="2026 MLB AL Champion",
        kalshi_event_ticker="KXMLBAL-26",
        poly_slug_prefix="tec-mlb-alchamp",
        match_kind="team",
        kalshi_to_poly_alias={"NYY": "nyy", "ATH": "ath"},
    ),
    SeriesRule(
        key="mlb_nl_champion_2026",
        label="2026 MLB NL Champion",
        kalshi_event_ticker="KXMLBNL-26",
        poly_slug_prefix="tec-mlb-nlchamp",
        match_kind="team",
        kalshi_to_poly_alias={"NYM": "nym", "AZ": "az"},
    ),
    SeriesRule(
        key="mls_winner_2026",
        label="2026 MLS Champion",
        kalshi_event_ticker="KXMLS-26",
        poly_slug_prefix="tec-mls-winner",
        match_kind="team",
    ),
    SeriesRule(
        key="epl_winner_2026",
        label="EPL 2025-26 Champion",
        kalshi_event_ticker="KXEPL-26",
        poly_slug_prefix="tec-epl-winner",
        match_kind="team",
    ),
    # --- Player-based: match by normalized name ---
    SeriesRule(
        key="nhl_hart_2026",
        label="NHL Hart Memorial Trophy 2026",
        kalshi_event_ticker="KXNHLHART-26",
        poly_slug_prefix="tec-nhl-hart",
        match_kind="player",
    ),
    SeriesRule(
        key="mlb_al_mvp_2026",
        label="MLB AL MVP 2026",
        kalshi_event_ticker="KXMLBALMVP-26",
        poly_slug_prefix="tec-mlb-almvp",
        match_kind="player",
        skip_kalshi_outcomes={"TIE"},
    ),
    SeriesRule(
        key="mlb_nl_mvp_2026",
        label="MLB NL MVP 2026",
        kalshi_event_ticker="KXMLBNLMVP-26",
        poly_slug_prefix="tec-mlb-nlmvp",
        match_kind="player",
        skip_kalshi_outcomes={"TIE"},
    ),
    SeriesRule(
        key="wnba_mvp_2026",
        label="WNBA MVP 2026",
        kalshi_event_ticker="KXWNBAMVP-26",
        poly_slug_prefix="tec-wnba-mvp",
        match_kind="player",
        skip_kalshi_outcomes={"TIE"},
    ),
    SeriesRule(
        key="fifa_wc_2026",
        label="FIFA World Cup 2026",
        kalshi_event_ticker="KXFIFAWC-26",
        poly_slug_prefix="tec-fifa-wc",
        match_kind="team",
    ),
]


# ---- fetchers --------------------------------------------------------------

def _kget(path: str, params: dict | None = None) -> dict:
    url = _KALSHI_BASE + path
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _fetch_kalshi_event_markets(event_ticker: str) -> list[dict]:
    try:
        res = _kget("/markets", {"event_ticker": event_ticker, "limit": 200, "status": "open"})
        return res.get("markets", []) or []
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        log.warning("kalshi %s: %s", event_ticker, e)
        return []


def _fetch_poly_markets_for_prefix(client: PolymarketUSClient, prefix: str) -> list[dict]:
    """Pull all Poly markets whose slug starts with `prefix`. The list endpoint
    doesn't have a slug-prefix filter, so we pull the broader catalog
    and filter locally. Cached the first time for cheap re-use."""
    if not _POLY_CACHE.get("markets"):
        all_markets: list[dict] = []
        cursor = None
        for _ in range(30):
            params = {"limit": 200, "closed": False, "active": True}
            if cursor:
                params["cursor"] = cursor
            try:
                r = client._client.markets.list(params)
            except PolymarketUSError as e:
                log.warning("polymarket list err: %s", e)
                break
            ms = r.get("markets", [])
            if not ms:
                break
            all_markets.extend(ms)
            cursor = r.get("nextCursor")
            if not cursor or r.get("eof"):
                break
        _POLY_CACHE["markets"] = all_markets
        _POLY_CACHE["ts"] = time.time()
    return [m for m in _POLY_CACHE["markets"] if (m.get("slug") or "").startswith(prefix)]


_POLY_CACHE: dict = {}


def clear_poly_cache() -> None:
    _POLY_CACHE.clear()


# ---- name normalization (for player matching) -----------------------------

def _normalize_name(s: str) -> str:
    """Lowercase, strip accents/punctuation, collapse whitespace."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _kalshi_player_name(market: dict) -> str:
    """Extract the player name from a Kalshi player market — preferring
    yes_sub_title (e.g. 'Auston Matthews') over the event-prefixed title."""
    name = (market.get("yes_sub_title") or market.get("subtitle") or "").strip()
    if name:
        return name
    # Fallback: pull from the title which is typically "Series — Player Name".
    title = market.get("title", "")
    if "—" in title:
        return title.split("—", 1)[1].strip()
    if "-" in title and title.count("-") >= 2:
        return title.rsplit("-", 1)[-1].strip()
    return title


def _poly_player_name(market: dict) -> str:
    """Extract the player name from a Polymarket player market — they put
    the full name in the `question` ('Will Connor McDavid win the Hart
    Memorial Trophy?')."""
    q = market.get("question", "") or ""
    # Strip the leading 'Will ' and trailing '?' / qualifier.
    m = re.match(r"^(?:will\s+)?(.+?)\s+(?:win|be\s+the|finish)\b", q, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return q.strip()


# ---- pair matching ---------------------------------------------------------

@dataclass
class PairMatch:
    series_key: str
    label: str
    kalshi_ticker: str
    polymarket_us_slug: str
    yes_means: str
    kalshi_outcome: str   # for telemetry/logging
    poly_outcome: str
    matched_by: str       # 'team-code' or 'player-name'


def _team_code_from_kalshi(ticker: str, event_ticker: str) -> str:
    """Strip `{event_ticker}-` off the front, return the rest."""
    if ticker.startswith(event_ticker + "-"):
        return ticker[len(event_ticker) + 1:]
    return ticker.rsplit("-", 1)[-1]


def _team_code_from_poly(slug: str, prefix: str) -> str:
    """Last hyphen segment of the slug — the outcome code."""
    return slug.rsplit("-", 1)[-1]


def match_series(rule: SeriesRule, kalshi_markets: list[dict],
                 poly_markets: list[dict]) -> list[PairMatch]:
    out: list[PairMatch] = []
    if not kalshi_markets or not poly_markets:
        return out

    skip = rule.skip_kalshi_outcomes or set()

    if rule.match_kind == "team":
        # Index Poly outcomes by lowercase code.
        poly_by_code: dict[str, dict] = {}
        for m in poly_markets:
            code = _team_code_from_poly(m.get("slug", ""), rule.poly_slug_prefix).lower()
            poly_by_code[code] = m

        aliases = rule.kalshi_to_poly_alias or {}
        for km in kalshi_markets:
            ticker = km.get("ticker", "")
            k_code = _team_code_from_kalshi(ticker, rule.kalshi_event_ticker)
            if k_code in skip:
                continue
            # Prefer alias, then direct lowercase match.
            target = aliases.get(k_code, k_code.lower())
            pm = poly_by_code.get(target)
            if not pm:
                continue
            out.append(PairMatch(
                series_key=rule.key,
                label=f"{rule.label} — {km.get('yes_sub_title') or k_code}",
                kalshi_ticker=ticker,
                polymarket_us_slug=pm.get("slug", ""),
                yes_means=rule.yes_means,
                kalshi_outcome=k_code,
                poly_outcome=target,
                matched_by="team-code",
            ))
        return out

    # Player matching
    poly_by_name: dict[str, dict] = {}
    for m in poly_markets:
        name = _normalize_name(_poly_player_name(m))
        if name:
            poly_by_name[name] = m

    for km in kalshi_markets:
        ticker = km.get("ticker", "")
        k_code = _team_code_from_kalshi(ticker, rule.kalshi_event_ticker)
        if k_code in skip:
            continue
        name = _normalize_name(_kalshi_player_name(km))
        if not name:
            continue
        pm = poly_by_name.get(name)
        if not pm:
            # Try a last-name-only fallback: some Polymarket questions phrase
            # as "Will Auston Matthews win" while Kalshi has "Auston Matthews"
            # so the direct hit is normal. If that fails, last name only:
            last = name.split()[-1] if name else ""
            for k, v in poly_by_name.items():
                if last and (k.split()[-1] == last):
                    pm = v; break
        if not pm:
            continue
        out.append(PairMatch(
            series_key=rule.key,
            label=f"{rule.label} — {_kalshi_player_name(km)}",
            kalshi_ticker=ticker,
            polymarket_us_slug=pm.get("slug", ""),
            yes_means=rule.yes_means,
            kalshi_outcome=k_code,
            poly_outcome=name,
            matched_by="player-name",
        ))
    return out


def run_rule_match(client: PolymarketUSClient | None = None) -> list[PairMatch]:
    """Iterate every SeriesRule and return all matched pairs."""
    pc = client or PolymarketUSClient()
    clear_poly_cache()
    matches: list[PairMatch] = []
    for rule in SERIES:
        kalshi_ms = _fetch_kalshi_event_markets(rule.kalshi_event_ticker)
        poly_ms = _fetch_poly_markets_for_prefix(pc, rule.poly_slug_prefix)
        these = match_series(rule, kalshi_ms, poly_ms)
        log.info("series %s: kalshi=%d poly=%d -> %d matches",
                 rule.key, len(kalshi_ms), len(poly_ms), len(these))
        matches.extend(these)
        time.sleep(0.25)
    return matches
