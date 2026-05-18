"""
Cross-venue market discovery + matching.

Two-stage pipeline:

  1. Pull catalogs from both venues (Kalshi via public REST,
     Polymarket via the SDK's markets.list).

  2. Pre-filter: for each Polymarket market, score every Kalshi market
     by title+rules text similarity and keep the top K candidates. Cheap
     and runs entirely locally — no API calls.

  3. LLM verification: for each surviving (poly, kalshi) candidate pair,
     ask Claude to read both sides' resolution criteria and end dates
     and decide:
        - is this the same underlying event?
        - is "YES" on one side equivalent to "YES" on the other, or
          inverted?
        - confidence 0-100
        - one-line reason

  4. Output: write discovered_pairs.json with the verified matches.
     A separate UI promote step copies a pair into markets.yaml so the
     scanner picks it up.

Run as a script or via the /api/discovery/* endpoints in main.py.
Background-friendly: the heavy work runs in a thread.

Env vars:
  ANTHROPIC_API_KEY      required for LLM verification. Without it,
                         discovery returns pre-filter candidates only
                         (with a similarity score instead of LLM
                         confidence).
  DISCOVERY_TOP_K        candidates per Poly market to LLM-verify.
                         Default 5.
  DISCOVERY_MODEL        Anthropic model ID. Default claude-haiku-4-5.
  DISCOVERY_CACHE_PATH   where to write discovered_pairs.json.
                         Default ./discovered_pairs.json.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from polymarket_us_client import PolymarketUSClient, PolymarketUSError

log = logging.getLogger("discovery")

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
_UA = "Mozilla/5.0 (compatible; ArbScanner-Discovery)"

TOP_K = int(os.getenv("DISCOVERY_TOP_K", "5"))
DEFAULT_MODEL = os.getenv("DISCOVERY_MODEL", "claude-haiku-4-5")
CACHE_PATH = Path(os.getenv("DISCOVERY_CACHE_PATH", "discovered_pairs.json"))


# ---- data types ----------------------------------------------------------

@dataclass
class MarketEntry:
    venue: str          # "kalshi" | "polymarket_us"
    market_id: str      # Kalshi ticker or Polymarket slug
    title: str
    rules: str          # resolution criteria text (rules_primary on Kalshi, description on Poly)
    end_date: str | None = None
    raw: dict | None = None

    def text_for_matching(self) -> str:
        return f"{self.title}\n{self.rules[:1500]}"


@dataclass
class Candidate:
    poly: MarketEntry
    kalshi: MarketEntry
    prefilter_score: float
    llm_match: bool | None = None       # None until LLM verifies
    llm_inverted: bool | None = None    # True if poly YES = kalshi NO
    llm_confidence: int | None = None   # 0-100
    llm_reason: str | None = None
    llm_error: str | None = None

    def as_dict(self) -> dict:
        return {
            "poly": {"market_id": self.poly.market_id, "title": self.poly.title,
                     "end_date": self.poly.end_date},
            "kalshi": {"market_id": self.kalshi.market_id, "title": self.kalshi.title,
                       "end_date": self.kalshi.end_date},
            "prefilter_score": self.prefilter_score,
            "llm_match": self.llm_match,
            "llm_inverted": self.llm_inverted,
            "llm_confidence": self.llm_confidence,
            "llm_reason": self.llm_reason,
            "llm_error": self.llm_error,
        }


# ---- catalog pull --------------------------------------------------------

def _kget(path: str, params: dict | None = None) -> dict:
    url = f"{KALSHI_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": _UA,
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def pull_kalshi_markets(max_events: int = 6000, throttle_sec: float = 0.25) -> list[MarketEntry]:
    """Pull every open Kalshi event, then fetch markets per event with
    rules_primary populated. Returns a flat list of MarketEntry."""
    events: list[dict] = []
    cursor = None
    for _ in range(60):
        p = {"limit": 200, "status": "open"}
        if cursor:
            p["cursor"] = cursor
        try:
            res = _kget("/events", p)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2.0)
                continue
            log.warning("kalshi /events err: %s", e)
            break
        evs = res.get("events", [])
        if not evs:
            break
        events.extend(evs)
        cursor = res.get("cursor")
        if not cursor or len(events) >= max_events:
            break
        time.sleep(throttle_sec)
    log.info("pulled %d kalshi events", len(events))

    # Use the /markets endpoint with event_ticker — returns markets with
    # rules_primary populated; one call per event keeps responses small.
    markets: list[MarketEntry] = []
    for i, ev in enumerate(events):
        ev_ticker = ev.get("event_ticker")
        if not ev_ticker:
            continue
        try:
            res = _kget("/markets", {"event_ticker": ev_ticker, "limit": 100, "status": "open"})
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2.0)
                continue
            continue
        ev_title = ev.get("title") or ""
        ev_category = ev.get("category") or ""
        for m in res.get("markets", []):
            title_parts = [ev_title]
            yes_sub = m.get("yes_sub_title") or m.get("subtitle") or ""
            if yes_sub:
                title_parts.append(yes_sub)
            title = " — ".join(p for p in title_parts if p)
            rules = (m.get("rules_primary") or "") + "\n" + (m.get("rules_secondary") or "")
            markets.append(MarketEntry(
                venue="kalshi",
                market_id=m.get("ticker") or "",
                title=title.strip(),
                rules=rules.strip(),
                end_date=(m.get("expiration_time") or m.get("close_time") or
                          ev.get("strike_date")),
                raw={"category": ev_category, "event_ticker": ev_ticker},
            ))
        if i % 50 == 0:
            log.info("kalshi: %d/%d events, %d markets so far",
                     i + 1, len(events), len(markets))
        time.sleep(throttle_sec)
    log.info("pulled %d kalshi markets", len(markets))
    return markets


def pull_poly_markets(client: PolymarketUSClient | None = None,
                      max_pages: int = 20,
                      throttle_sec: float = 0.1) -> list[MarketEntry]:
    """Pull every open Polymarket US market via the SDK. Public read."""
    pc = client or PolymarketUSClient()
    markets: list[MarketEntry] = []
    cursor = None
    for _ in range(max_pages):
        params = {"limit": 200, "closed": False, "active": True}
        if cursor:
            params["cursor"] = cursor
        try:
            res = pc._client.markets.list(params)
        except PolymarketUSError as e:
            log.warning("polymarket markets.list err: %s", e)
            break
        ms = res.get("markets", [])
        if not ms:
            break
        for m in ms:
            markets.append(MarketEntry(
                venue="polymarket_us",
                market_id=m.get("slug") or "",
                title=(m.get("question") or "").strip(),
                rules=(m.get("description") or "").strip(),
                end_date=m.get("endDate"),
                raw={"category": m.get("category"), "id": m.get("id")},
            ))
        cursor = res.get("nextCursor")
        if not cursor or res.get("eof"):
            break
        time.sleep(throttle_sec)
    log.info("pulled %d polymarket markets", len(markets))
    return markets


# ---- pre-filter via TF-IDF -----------------------------------------------

_TOK_RE = re.compile(r"[A-Za-z0-9]+")


def _tokenize(s: str) -> list[str]:
    return [t.lower() for t in _TOK_RE.findall(s)]


_STOP = {
    "the", "a", "an", "will", "be", "of", "to", "in", "on", "by", "and", "or",
    "for", "at", "with", "is", "are", "this", "that", "next", "before", "after",
    "win", "winner", "wins", "yes", "no",
}


def _build_idf(docs: list[list[str]]) -> dict[str, float]:
    n = len(docs)
    df: Counter = Counter()
    for d in docs:
        for tok in set(d):
            df[tok] += 1
    return {tok: math.log((1.0 + n) / (1.0 + df_)) + 1.0 for tok, df_ in df.items()}


def _tfidf_vec(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    tf = Counter(tokens)
    vec = {t: (count / len(tokens)) * idf.get(t, 0.0) for t, count in tf.items() if t not in _STOP}
    # Normalize
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {t: v / norm for t, v in vec.items()}


def _cos(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(t, 0.0) for t, v in a.items())


def prefilter_candidates(
    poly_markets: list[MarketEntry],
    kalshi_markets: list[MarketEntry],
    top_k: int = TOP_K,
) -> list[Candidate]:
    """For each Polymarket market, find the top-K most similar Kalshi
    markets by TF-IDF cosine on title+rules text. Returns a flat list
    of Candidate (each Poly market generates up to top_k entries)."""
    poly_docs = [_tokenize(m.text_for_matching()) for m in poly_markets]
    kalshi_docs = [_tokenize(m.text_for_matching()) for m in kalshi_markets]
    idf = _build_idf(poly_docs + kalshi_docs)
    poly_vecs = [_tfidf_vec(d, idf) for d in poly_docs]
    kalshi_vecs = [_tfidf_vec(d, idf) for d in kalshi_docs]

    out: list[Candidate] = []
    for pi, pvec in enumerate(poly_vecs):
        if not pvec:
            continue
        scores: list[tuple[float, int]] = []
        for ki, kvec in enumerate(kalshi_vecs):
            s = _cos(pvec, kvec)
            if s > 0.05:
                scores.append((s, ki))
        scores.sort(reverse=True)
        for s, ki in scores[:top_k]:
            out.append(Candidate(
                poly=poly_markets[pi],
                kalshi=kalshi_markets[ki],
                prefilter_score=round(s, 4),
            ))
    return out


# ---- LLM verification ----------------------------------------------------

_LLM_SYSTEM = """You compare two prediction-market binary questions, one from \
Kalshi and one from Polymarket US, and decide whether they pay out on the same \
underlying event.

Return STRICT JSON with these fields:
  match (bool): true iff a YES win on side A and a YES win on side B both \
require essentially the same real-world outcome to happen.
  inverted_yes (bool): true iff a YES on Polymarket equals a NO on Kalshi \
(or vice versa). Only meaningful when match is true OR when the two questions \
clearly track inverse outcomes of the same event.
  confidence (int 0-100): how certain you are.
  reason (string, ≤120 chars): one short sentence explaining the call.

Examples of NON-matches that look superficially similar:
  - Same general topic but different resolution dates (e.g. 2026 election vs 2028 election).
  - Same event but different exact resolution criteria (e.g. "wins regular season" vs "wins championship").
  - Same league but different teams or players.

Be strict. False positives are worse than false negatives in this system.
Output ONLY valid JSON. No markdown, no preamble."""


_LLM_USER_TEMPLATE = """KALSHI market
ticker: {k_ticker}
title:  {k_title}
end:    {k_end}
rules:  {k_rules}

POLYMARKET US market
slug:   {p_slug}
title:  {p_title}
end:    {p_end}
rules:  {p_rules}

Do these resolve on the same underlying event?"""


def _llm_verify_one(cand: Candidate, client, model: str) -> Candidate:
    user = _LLM_USER_TEMPLATE.format(
        k_ticker=cand.kalshi.market_id,
        k_title=cand.kalshi.title[:200],
        k_end=cand.kalshi.end_date or "?",
        k_rules=cand.kalshi.rules[:1200],
        p_slug=cand.poly.market_id,
        p_title=cand.poly.title[:200],
        p_end=cand.poly.end_date or "?",
        p_rules=cand.poly.rules[:1200],
    )
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=300,
            system=_LLM_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
        # The model occasionally wraps in ```json — strip.
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        data = json.loads(text)
        cand.llm_match = bool(data.get("match"))
        cand.llm_inverted = bool(data.get("inverted_yes"))
        try:
            cand.llm_confidence = int(data.get("confidence") or 0)
        except (TypeError, ValueError):
            cand.llm_confidence = 0
        cand.llm_reason = str(data.get("reason") or "")[:200]
    except Exception as e:
        cand.llm_error = f"{type(e).__name__}: {e}"
    return cand


def verify_candidates(candidates: list[Candidate], model: str | None = None) -> list[Candidate]:
    """LLM-verify every candidate. Returns the same list with the
    llm_* fields populated. Requires ANTHROPIC_API_KEY."""
    if not candidates:
        return candidates
    try:
        from anthropic import Anthropic
    except ImportError:
        log.warning("anthropic SDK not installed — skipping LLM verification")
        for c in candidates:
            c.llm_error = "anthropic SDK not installed"
        return candidates
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        log.warning("ANTHROPIC_API_KEY not set — skipping LLM verification")
        for c in candidates:
            c.llm_error = "ANTHROPIC_API_KEY not set"
        return candidates
    client = Anthropic(api_key=api_key)
    mdl = model or DEFAULT_MODEL
    for i, cand in enumerate(candidates):
        _llm_verify_one(cand, client, mdl)
        if i % 25 == 0:
            log.info("LLM verified %d / %d", i + 1, len(candidates))
    return candidates


# ---- entry point ---------------------------------------------------------

def run_discovery(top_k: int = TOP_K, do_llm: bool = True,
                  cache_path: Path = CACHE_PATH) -> dict:
    """Run the full pipeline. Writes cache_path on completion. Returns
    a summary dict (also includes the candidates)."""
    started = time.time()
    log.info("discovery: pulling catalogs")
    kalshi_markets = pull_kalshi_markets()
    poly_markets = pull_poly_markets()
    log.info("discovery: prefilter")
    cands = prefilter_candidates(poly_markets, kalshi_markets, top_k=top_k)
    log.info("discovery: %d candidates", len(cands))
    if do_llm:
        log.info("discovery: LLM verification")
        cands = verify_candidates(cands)
    # Sort by (match desc, confidence desc, prefilter desc)
    cands.sort(key=lambda c: (
        1 if c.llm_match else 0,
        c.llm_confidence or 0,
        c.prefilter_score,
    ), reverse=True)
    result = {
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_sec": round(time.time() - started, 1),
        "kalshi_count": len(kalshi_markets),
        "poly_count": len(poly_markets),
        "candidate_count": len(cands),
        "match_count": sum(1 for c in cands if c.llm_match),
        "candidates": [c.as_dict() for c in cands],
    }
    try:
        cache_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning("failed to write %s: %s", cache_path, e)
    log.info("discovery done in %.1fs: %d markets x %d -> %d candidates, %d matches",
             result["elapsed_sec"], result["poly_count"], result["kalshi_count"],
             result["candidate_count"], result["match_count"])
    return result


def load_cached() -> dict:
    if not CACHE_PATH.exists():
        return {"fetched_at": None, "candidates": []}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"fetched_at": None, "candidates": []}
