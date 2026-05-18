"""
FastAPI app: Kalshi <-> Polymarket US arbitrage scanner + portfolio +
two-leg execution.

DRY_RUN behavior:
    - If env var DRY_RUN is "1" / "true" / unset, /api/arb/execute returns
      a synthetic "filled at quoted price" result without hitting either
      venue's order API.
    - When ready to go live, set DRY_RUN=false in Railway env vars and
      restart the service. Verify both KALSHI_* and POLYMARKET_US_* envs
      are set, the configured pairs in markets.yaml are real, and start
      with a small MAX_ORDER_USD.

Required env vars (deploy):
    APP_PASSWORD              shared password for HTTP Basic auth
    KALSHI_KEY_ID
    KALSHI_PRIVATE_KEY        PEM (literal newlines or \\n)
    POLYMARKET_US_KEY_ID
    POLYMARKET_US_SECRET_KEY  Base64 Ed25519 32-byte private key

Optional env vars:
    DRY_RUN                   "true" (default) = stub orders; "false" = live
    MAX_ORDER_USD             per-execution combined-notional cap. Default 50.
    MARKETS_REGISTRY_PATH     path to markets.yaml. Default ./markets.yaml
    POLY_US_TAKER_FEE_BPS     basis points. Default 10 (0.10%).
    MAX_HEDGE_SLIPPAGE_C      max cents/contract overpay when hedging. Default 2.
    NTFY_TOPIC                ntfy.sh topic for push alerts
    NTFY_SERVER               default https://ntfy.sh
    NTFY_ARB_SPREAD_C         alert when locked spread >= N cents/contract. Default 1.0.
    NTFY_COOLDOWN_SEC         min seconds between alerts for same opportunity. Default 300.
    NTFY_INTERVAL_SEC         background scan interval. Default 30.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import discovery
import rule_match
import scanner
from arb_executor import execute_arb
from kalshi_client import KalshiClient, KalshiError, KalshiNotConfigured
from polymarket_us_client import (
    PolymarketUSClient,
    PolymarketUSError,
    PolymarketUSNotConfigured,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("arb")

APP_PASSWORD = os.getenv("APP_PASSWORD", "")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() not in ("0", "false", "no", "off")
MAX_ORDER_USD = float(os.getenv("MAX_ORDER_USD", "50"))

# ntfy background push
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_ARB_SPREAD_C = float(os.getenv("NTFY_ARB_SPREAD_C", "1.0"))
NTFY_COOLDOWN_SEC = int(os.getenv("NTFY_COOLDOWN_SEC", "300"))
NTFY_INTERVAL_SEC = int(os.getenv("NTFY_INTERVAL_SEC", "30"))

# Background discovery scheduler. Off by default — set
# DISCOVERY_INTERVAL_HOURS=6 to refresh candidates every 6 hours, and
# DISCOVERY_AUTO_RUN_ON_START=true to also kick a run on app boot.
# Each full run hits the Anthropic API and your throttled Kalshi quota;
# 6h is a reasonable cadence for catching new/expiring markets.
DISCOVERY_INTERVAL_HOURS = float(os.getenv("DISCOVERY_INTERVAL_HOURS", "0"))
DISCOVERY_AUTO_RUN_ON_START = (
    os.getenv("DISCOVERY_AUTO_RUN_ON_START", "false").lower()
    in ("1", "true", "yes", "on")
)

if not APP_PASSWORD:
    log.warning("APP_PASSWORD not set — app is OPEN. Do not deploy like this.")
if DRY_RUN:
    log.warning("DRY_RUN enabled — /api/arb/execute will NOT place real orders.")
if NTFY_TOPIC:
    log.info(
        "ntfy push enabled: server=%s, interval=%ss, arb_spread>=%.1fc, cooldown=%ss",
        NTFY_SERVER, NTFY_INTERVAL_SEC, NTFY_ARB_SPREAD_C, NTFY_COOLDOWN_SEC,
    )
else:
    log.info("NTFY_TOPIC not set — background push disabled.")


# ---- shared clients --------------------------------------------------------

kalshi = KalshiClient()
poly = PolymarketUSClient()


# ---- ntfy push -------------------------------------------------------------

# Rising-edge state per opportunity (pair_key + direction). Push fires
# only on False->True transition + min refire gap (NTFY_COOLDOWN_SEC).
_ntfy_alert_state: dict[str, dict] = {}
_ntfy_stop = threading.Event()


def _ntfy_should_fire(key: str, condition_met: bool) -> bool:
    st = _ntfy_alert_state.setdefault(key, {"above": False, "last_fire_at": 0.0})
    now = time.time()
    if not condition_met:
        st["above"] = False
        return False
    rising = not st["above"]
    long_gap = now - st["last_fire_at"] > NTFY_COOLDOWN_SEC
    st["above"] = True
    if rising or long_gap:
        st["last_fire_at"] = now
        return True
    return False


def ntfy_send(message: str, *, title: str | None = None,
              priority: str = "default",
              tags: list[str] | None = None) -> tuple[bool, str]:
    if not NTFY_TOPIC:
        return False, "NTFY_TOPIC not set"
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    headers: dict[str, str] = {"Content-Type": "text/plain; charset=utf-8"}
    if title:
        headers["Title"] = title.encode("ascii", "replace").decode("ascii")
    if priority:
        headers["Priority"] = priority
    if tags:
        headers["Tags"] = ",".join(tags)
    req = urllib.request.Request(
        url, data=message.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        return True, ""
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        return False, str(e)


def _check_opportunities_and_push() -> dict:
    summary = {"alerts_pushed": 0, "errors": []}
    try:
        scan_data = scanner.run_scan(contracts=100, min_spread_cents=NTFY_ARB_SPREAD_C)
    except Exception as e:
        summary["errors"].append(f"scan: {e}")
        return summary
    for r in scan_data.get("rows", []):
        spread_c = r["locked_spread_cents"]
        key = f"{r['pair_key']}:{r['direction']}"
        condition = spread_c >= NTFY_ARB_SPREAD_C
        if not _ntfy_should_fire(key, condition):
            continue
        kl = r["kalshi_leg"]; pl = r["poly_leg"]
        title = f"ARB {r['label']} +{spread_c:.1f}c/contract"
        body = (
            f"KX {kl['side'].upper()} {kl['price_cents']:.1f}c + "
            f"PM {pl['side'].upper()} {pl['price_cents']:.1f}c "
            f"-> lock {spread_c:.2f}c (fees {r['fees_total_cents']:.2f}c)"
        )
        ok, err = ntfy_send(body, title=title, priority="high",
                            tags=["green_circle", "moneybag"])
        if ok:
            summary["alerts_pushed"] += 1
        else:
            summary["errors"].append(f"push {key}: {err}")
    return summary


def _ntfy_loop() -> None:
    log.info("ntfy background loop started")
    while not _ntfy_stop.is_set():
        try:
            s = _check_opportunities_and_push()
            if s["alerts_pushed"] or s["errors"]:
                log.info(f"ntfy loop: {s}")
        except Exception:
            log.exception("ntfy loop error")
        _ntfy_stop.wait(NTFY_INTERVAL_SEC)
    log.info("ntfy background loop stopped")


_discovery_stop = threading.Event()


def _discovery_loop() -> None:
    """Periodic discovery refresh. Sleeps DISCOVERY_INTERVAL_HOURS between
    runs, kicking off a fresh catalog pull + LLM verify each cycle."""
    log.info("discovery background loop started (every %.1fh, auto_start=%s)",
             DISCOVERY_INTERVAL_HOURS, DISCOVERY_AUTO_RUN_ON_START)
    # First-run delay: if AUTO_RUN_ON_START, kick off after 60s grace.
    # Otherwise wait the full interval before the first run.
    first_wait = 60 if DISCOVERY_AUTO_RUN_ON_START else DISCOVERY_INTERVAL_HOURS * 3600
    if _discovery_stop.wait(first_wait):
        return
    while not _discovery_stop.is_set():
        try:
            if not _DISCOVERY_STATE["running"]:
                log.info("discovery scheduler: kicking off run")
                _discovery_worker(do_llm=True)
        except Exception:
            log.exception("discovery scheduler tick failed")
        # Sleep the interval before the next tick.
        if _discovery_stop.wait(DISCOVERY_INTERVAL_HOURS * 3600):
            return
    log.info("discovery background loop stopped")


# ---- app lifecycle --------------------------------------------------------

@asynccontextmanager
async def _lifespan(_: FastAPI):
    if NTFY_TOPIC:
        threading.Thread(target=_ntfy_loop, daemon=True, name="ntfy").start()
    if DISCOVERY_INTERVAL_HOURS > 0 or DISCOVERY_AUTO_RUN_ON_START:
        threading.Thread(target=_discovery_loop, daemon=True, name="discovery-loop").start()
    try:
        yield
    finally:
        _ntfy_stop.set()
        _discovery_stop.set()


app = FastAPI(title="Arb Scanner", version="1.0", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False,
    allow_methods=["*"], allow_headers=["*"],
)

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
ORDER_LOG_PATH = BASE_DIR / "orders.log.jsonl"


# ---- caches + auth --------------------------------------------------------

_SCAN_CACHE: dict[str, Any] = {}
_PENDING: dict[str, dict] = {}   # token -> pending execution intent

security = HTTPBasic(auto_error=False)


def require_auth(creds: HTTPBasicCredentials | None = Depends(security)) -> str:
    if not APP_PASSWORD:
        return "dev"
    if creds is None or not secrets.compare_digest(creds.password or "", APP_PASSWORD):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Auth required",
            headers={"WWW-Authenticate": 'Basic realm="Arb Scanner"'},
        )
    return creds.username or "user"


# ---- routes ---------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "ok": True,
        "dry_run": DRY_RUN,
        "kalshi_configured": kalshi.configured,
        "polymarket_us_configured": poly.configured,
        "auth_enabled": bool(APP_PASSWORD),
        "ntfy_configured": bool(NTFY_TOPIC),
        "ntfy_arb_spread_c": NTFY_ARB_SPREAD_C,
        "ntfy_interval_sec": NTFY_INTERVAL_SEC,
        "max_order_usd": MAX_ORDER_USD,
        "poly_us_taker_fee_bps": scanner.POLY_US_TAKER_FEE_BPS,
        "server_time_utc": datetime.utcnow().isoformat() + "Z",
    }


@app.post("/api/ntfy/test")
def api_ntfy_test(_: str = Depends(require_auth)):
    if not NTFY_TOPIC:
        raise HTTPException(503, "NTFY_TOPIC not set in env vars")
    ok, err = ntfy_send(
        "If you got this, server -> ntfy.sh -> your phone is wired up.",
        title="Arb Scanner: test push",
        tags=["white_check_mark"],
    )
    if not ok:
        raise HTTPException(502, f"ntfy push failed: {err}")
    return {"ok": True, "server": NTFY_SERVER}


@app.get("/")
def index(_: str = Depends(require_auth)):
    return FileResponse(STATIC_DIR / "index.html")


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---- /api/scan ------------------------------------------------------------

@app.get("/api/scan")
def api_scan(contracts: int = 100,
             min_spread: float = -5.0,
             min_annualized: float | None = None,
             _: str = Depends(require_auth)):
    """Return all registered pairs with their current arb spread.

    min_spread:     keep rows with locked_spread_cents >= this many ¢ (default -5)
    min_annualized: if set, additionally keep only rows with annualized_return_pct
                    >= this value. Rows with unknown end_date pass through.
    """
    now = time.time()
    cache_key = f"{contracts}:{min_spread}:{min_annualized}"
    cached = _SCAN_CACHE.get(cache_key)
    if cached and now - cached["ts"] < 5.0:
        return cached["data"]
    data = scanner.run_scan(contracts=contracts,
                            min_spread_cents=min_spread,
                            min_annualized_pct=min_annualized)
    data["fetched_at"] = datetime.utcnow().isoformat() + "Z"
    _SCAN_CACHE[cache_key] = {"ts": now, "data": data}
    return data


# ---- /api/positions -------------------------------------------------------

def _f(v, default: float = 0.0) -> float:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _kalshi_position_for_ticker(positions_resp: dict, ticker: str) -> dict:
    """Find the Kalshi position dict for one ticker, or {} if none."""
    raw = positions_resp.get("market_positions") or positions_resp.get("positions") or []
    for p in raw:
        if (p.get("ticker") or "").upper() == ticker.upper():
            return p
    return {}


def _poly_position_for_slug(positions_resp: dict, slug: str) -> dict:
    """Find the Polymarket position dict for one slug.

    The SDK's GetUserPositionsResponse is `{"positions": {slug: UserPosition}}`
    — the slug is the dict key, not a field on the position.
    """
    if not isinstance(positions_resp, dict):
        return {}
    nested = positions_resp.get("positions")
    if isinstance(nested, dict) and slug in nested:
        return nested[slug] or {}
    # Tolerate the rarer shapes too (top-level map or list).
    if slug in positions_resp and isinstance(positions_resp[slug], dict):
        return positions_resp[slug]
    if isinstance(nested, list):
        for p in nested:
            md = p.get("marketMetadata") or {}
            if (md.get("slug") or p.get("slug") or "").lower() == slug.lower():
                return p
    return {}


def _amount_value(amt: Any) -> float:
    """Pull a float out of a Polymarket Amount object {value: str, currency}."""
    if isinstance(amt, dict):
        return _f(amt.get("value"))
    return _f(amt)


def _summarize_poly_balance(resp: dict) -> dict:
    """Pull headline numbers out of a GetAccountBalancesResponse.

    Shape: `{"balances": [UserBalance, ...]}`. UserBalance has
    currentBalance, assetNotional, buyingPower, etc — all floats in USD.
    For Polymarket US there's usually only the USD entry.
    """
    balances = (resp or {}).get("balances") or []
    if not balances:
        return {
            "cash_dollars": 0.0,
            "portfolio_value_dollars": 0.0,
            "buying_power_dollars": 0.0,
            "total_dollars": 0.0,
            "raw": resp or None,
        }
    # Prefer the USD entry; fall back to the first if currency isn't tagged.
    usd = next((b for b in balances if (b.get("currency") or "").upper() == "USD"),
               balances[0])
    # buyingPower = the spendable number Polymarket shows in their UI.
    # currentBalance = total account value (cash + assets). assetAvailable
    # is the post-position cash and is 0 for a fresh account because
    # Polymarket reserves the full balance until trading begins; using
    # buyingPower for the headline cash matches what users see in the app.
    return {
        "cash_dollars": _f(usd.get("buyingPower")),
        "portfolio_value_dollars": _f(usd.get("assetNotional")),
        "buying_power_dollars": _f(usd.get("buyingPower")),
        "total_dollars": _f(usd.get("currentBalance")),
        "raw": resp or None,
    }


@app.get("/api/positions")
def api_positions(_: str = Depends(require_auth)):
    """Per-pair view: for each registered pair, fetch holdings on both
    venues and report the combined position.

    Every section is wrapped so a failure on one side (or in our parsing)
    never 500s the endpoint — it surfaces in `errors[]` instead, so the
    UI can render partial data.
    """
    import traceback
    kalshi_balance: dict = {}
    kalshi_positions: dict = {}
    poly_balances: dict = {}
    poly_positions: dict = {}
    errors: list[str] = []

    if kalshi.configured:
        try:
            kalshi_balance = kalshi.get_balance() or {}
        except Exception as e:
            log.exception("kalshi.get_balance failed")
            errors.append(f"kalshi balance: {type(e).__name__}: {e}")
        try:
            kalshi_positions = kalshi.get_positions(limit=200) or {}
        except Exception as e:
            log.exception("kalshi.get_positions failed")
            errors.append(f"kalshi positions: {type(e).__name__}: {e}")

    if poly.configured:
        try:
            poly_balances = poly.get_balance() or {}
        except Exception as e:
            log.exception("poly.get_balance failed")
            errors.append(f"polymarket balance: {type(e).__name__}: {e}")
        try:
            poly_positions = poly.get_positions() or {}
        except Exception as e:
            log.exception("poly.get_positions failed")
            errors.append(f"polymarket positions: {type(e).__name__}: {e}")

    rows = []
    try:
        pairs = scanner.load_registry()
    except Exception as e:
        log.exception("load_registry failed")
        errors.append(f"registry: {type(e).__name__}: {e}")
        pairs = []

    for cfg in pairs:
        try:
            kpos = _kalshi_position_for_ticker(kalshi_positions, cfg.kalshi_ticker)
            ppos = _poly_position_for_slug(poly_positions, cfg.polymarket_us_slug)
            kshares = _f(kpos.get("position_fp") or kpos.get("position"))
            kside = "yes" if kshares > 0 else ("no" if kshares < 0 else None)
            pshares = _f(ppos.get("netPosition"))
            pmd = ppos.get("marketMetadata") or {}
            pside = (pmd.get("outcome") or "").lower() or None
            rows.append({
                "key": cfg.key,
                "label": cfg.label,
                "kalshi_ticker": cfg.kalshi_ticker,
                "polymarket_us_slug": cfg.polymarket_us_slug,
                "enabled": cfg.enabled,
                "kalshi": {
                    "shares": kshares,
                    "side": kside,
                    "exposure_dollars": _f(kpos.get("market_exposure_dollars")),
                    "realized_pnl_dollars": _f(kpos.get("realized_pnl_dollars")),
                    "fees_paid_dollars": _f(kpos.get("fees_paid_dollars")),
                },
                "polymarket": {
                    "shares": pshares,
                    "side": pside,
                    "cost_basis": _amount_value(ppos.get("cost")),
                    "realized_pnl": _amount_value(ppos.get("realized")),
                    "cash_value": _amount_value(ppos.get("cashValue")),
                },
            })
        except Exception as e:
            log.exception("pair %s row build failed", cfg.key)
            errors.append(f"pair {cfg.key}: {type(e).__name__}: {e}")

    # Kalshi balance: returned in cents.
    try:
        cash_k = (kalshi_balance.get("balance") or 0) / 100.0
        pv_k = (kalshi_balance.get("portfolio_value") or 0) / 100.0
    except Exception as e:
        errors.append(f"kalshi balance parse: {type(e).__name__}: {e}")
        cash_k = 0.0; pv_k = 0.0

    try:
        poly_summary = _summarize_poly_balance(poly_balances)
    except Exception as e:
        log.exception("poly balance summary failed")
        errors.append(f"polymarket balance parse: {type(e).__name__}: {e}")
        poly_summary = {"cash_dollars": 0.0, "portfolio_value_dollars": 0.0,
                        "buying_power_dollars": 0.0, "total_dollars": 0.0,
                        "raw": poly_balances}

    return {
        "kalshi_connected": kalshi.configured,
        "polymarket_us_connected": poly.configured,
        "kalshi_cash_dollars": cash_k,
        "kalshi_portfolio_value_dollars": pv_k,
        "polymarket_us": poly_summary,
        "pairs": rows,
        "errors": errors,
    }


@app.get("/api/debug/poly")
def api_debug_poly(_: str = Depends(require_auth)):
    """Diagnostic: dump raw Polymarket SDK responses + types so we can see
    what shape the server is actually getting back. Strip nothing — show
    the truth."""
    import traceback
    out: dict = {
        "configured": poly.configured,
        "has_sdk": True,
    }
    if not poly.configured:
        out["note"] = "POLYMARKET_US_KEY_ID / POLYMARKET_US_SECRET_KEY env vars not set"
        return out
    for name, fn in [
        ("balances", lambda: poly.get_balance()),
        ("positions", lambda: poly.get_positions()),
    ]:
        try:
            r = fn()
            out[name] = {
                "type": type(r).__name__,
                "value": r,
            }
        except Exception as e:
            out[name] = {
                "error_type": type(e).__name__,
                "error_msg": str(e),
                "traceback": traceback.format_exc(),
            }
    return out


# ---- /api/discovery/* -----------------------------------------------------

_DISCOVERY_STATE: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_summary": None,
    "error": None,
}
_DISCOVERY_LOCK = threading.Lock()


def _discovery_worker(do_llm: bool):
    with _DISCOVERY_LOCK:
        if _DISCOVERY_STATE["running"]:
            return
        _DISCOVERY_STATE["running"] = True
        _DISCOVERY_STATE["started_at"] = datetime.utcnow().isoformat() + "Z"
        _DISCOVERY_STATE["error"] = None
    try:
        summary = discovery.run_discovery(do_llm=do_llm)
        _DISCOVERY_STATE["last_summary"] = {
            "fetched_at": summary["fetched_at"],
            "elapsed_sec": summary["elapsed_sec"],
            "kalshi_count": summary["kalshi_count"],
            "poly_count": summary["poly_count"],
            "candidate_count": summary["candidate_count"],
            "match_count": summary["match_count"],
        }
    except Exception as e:
        log.exception("discovery worker failed")
        _DISCOVERY_STATE["error"] = f"{type(e).__name__}: {e}"
    finally:
        _DISCOVERY_STATE["finished_at"] = datetime.utcnow().isoformat() + "Z"
        _DISCOVERY_STATE["running"] = False


@app.post("/api/discovery/run")
def api_discovery_run(do_llm: bool = True, _: str = Depends(require_auth)):
    """Kick off a discovery run in a background thread. Returns
    immediately. Poll /api/discovery/status to track progress.
    """
    if _DISCOVERY_STATE["running"]:
        return {"ok": False, "reason": "already running",
                "started_at": _DISCOVERY_STATE["started_at"]}
    t = threading.Thread(
        target=_discovery_worker, kwargs={"do_llm": do_llm},
        daemon=True, name="discovery",
    )
    t.start()
    return {"ok": True, "started_at": datetime.utcnow().isoformat() + "Z",
            "do_llm": do_llm}


@app.get("/api/discovery/status")
def api_discovery_status(_: str = Depends(require_auth)):
    return {
        "running": _DISCOVERY_STATE["running"],
        "started_at": _DISCOVERY_STATE["started_at"],
        "finished_at": _DISCOVERY_STATE["finished_at"],
        "last_summary": _DISCOVERY_STATE["last_summary"],
        "error": _DISCOVERY_STATE["error"],
        "anthropic_configured": bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
    }


@app.get("/api/discovery/candidates")
def api_discovery_candidates(
    limit: int = 200,
    min_confidence: int = 0,
    matches_only: bool = False,
    _: str = Depends(require_auth),
):
    """Return cached candidates, optionally filtered.

    matches_only=true keeps only candidates the LLM flagged as match=true.
    min_confidence filters by llm_confidence (0-100).
    """
    cached = discovery.load_cached()
    candidates = cached.get("candidates") or []
    if matches_only:
        candidates = [c for c in candidates if c.get("llm_match")]
    if min_confidence > 0:
        candidates = [c for c in candidates if (c.get("llm_confidence") or 0) >= min_confidence]
    return {
        "fetched_at": cached.get("fetched_at"),
        "total": len(cached.get("candidates") or []),
        "shown": min(len(candidates), limit),
        "candidates": candidates[:limit],
    }


def _append_pairs_to_registry(new_pairs: list[dict]) -> dict:
    """Append a batch of pair dicts to markets.yaml. Idempotent —
    skips pairs whose (kalshi_ticker, polymarket_us_slug) already
    appear in the registry. Returns counts."""
    import yaml as _yaml
    path = Path(os.getenv("MARKETS_REGISTRY_PATH", "markets.yaml"))
    try:
        raw = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raw = {}
    existing = raw.get("pairs") or []
    seen = {(p.get("kalshi_ticker"), p.get("polymarket_us_slug"))
            for p in existing}
    added = 0
    for entry in new_pairs:
        key = (entry["kalshi_ticker"], entry["polymarket_us_slug"])
        if key in seen:
            continue
        existing.append(entry)
        seen.add(key)
        added += 1
    raw["pairs"] = existing
    path.write_text(_yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return {"added": added, "skipped": len(new_pairs) - added,
            "total_in_registry": len(existing)}


@app.post("/api/discovery/rule_match")
def api_discovery_rule_match(auto_promote: bool = True,
                             _: str = Depends(require_auth)):
    """Run the deterministic team/player matcher across the known sports
    series. Returns the matched pairs. With auto_promote=true (default),
    also appends them to markets.yaml so the scanner picks them up on
    the next tick.

    No LLM, no Anthropic key needed. Fast (<10s typical) — runs inline.
    """
    try:
        matches = rule_match.run_rule_match()
    except Exception as e:
        log.exception("rule_match failed")
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    new_pairs = [{
        "key": re.sub(r"[^a-z0-9]+", "-", m.kalshi_ticker.lower()).strip("-")[:48] or "pair",
        "label": m.label,
        "kalshi_ticker": m.kalshi_ticker,
        "polymarket_us_slug": m.polymarket_us_slug,
        "yes_means": m.yes_means,
        "enabled": True,
    } for m in matches]
    promote_result = None
    if auto_promote and new_pairs:
        promote_result = _append_pairs_to_registry(new_pairs)
    # Group counts by series for the response summary
    from collections import Counter as _C
    by_series = _C(m.series_key for m in matches)
    return {
        "ok": True,
        "matches": [{
            "series_key": m.series_key,
            "label": m.label,
            "kalshi_ticker": m.kalshi_ticker,
            "polymarket_us_slug": m.polymarket_us_slug,
            "matched_by": m.matched_by,
            "yes_means": m.yes_means,
        } for m in matches],
        "by_series": dict(by_series),
        "auto_promoted": promote_result,
    }


class PromoteBody(BaseModel):
    poly_slug: str
    kalshi_ticker: str
    label: str | None = None
    yes_means: str = Field(default="same", pattern="^(same|inverted)$")


@app.post("/api/discovery/promote")
def api_discovery_promote(body: PromoteBody, _: str = Depends(require_auth)):
    """Append a candidate pair to markets.yaml so the scanner picks it up.

    Idempotent: skips if a pair with the same kalshi_ticker +
    polymarket_us_slug already exists.
    """
    import yaml as _yaml
    path = Path(os.getenv("MARKETS_REGISTRY_PATH", "markets.yaml"))
    try:
        raw = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raw = {}
    pairs = raw.get("pairs") or []
    for p in pairs:
        if (p.get("kalshi_ticker") == body.kalshi_ticker
                and p.get("polymarket_us_slug") == body.poly_slug):
            return {"ok": True, "skipped": True, "reason": "already present"}
    # Derive a stable key from the kalshi ticker (lower + hyphens).
    derived_key = re.sub(r"[^a-z0-9]+", "-",
                         body.kalshi_ticker.lower()).strip("-")[:48] or "pair"
    new_pair = {
        "key": derived_key,
        "label": body.label or body.kalshi_ticker,
        "kalshi_ticker": body.kalshi_ticker,
        "polymarket_us_slug": body.poly_slug,
        "yes_means": body.yes_means,
        "enabled": True,
    }
    pairs.append(new_pair)
    raw["pairs"] = pairs
    path.write_text(_yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return {"ok": True, "skipped": False, "added": new_pair}


# `re` is imported at module top via the FastAPI imports above? Make sure:
import re  # noqa: E402,F811


# ---- /api/arb/recommend ---------------------------------------------------

class RecommendBody(BaseModel):
    pair_key: str
    direction: str = Field(pattern="^(A|B)$")
    contracts: int = Field(gt=0, le=10000)
    allow_over_cap: bool = False


@app.post("/api/arb/recommend")
def api_arb_recommend(body: RecommendBody, _: str = Depends(require_auth)):
    """Quote one arb opportunity. Returns a one-shot token consumable by
    /api/arb/execute within 60 seconds.
    """
    scan = scanner.run_scan(contracts=body.contracts, min_spread_cents=-99.0)
    opp = next(
        (r for r in scan["rows"]
         if r["pair_key"] == body.pair_key and r["direction"] == body.direction),
        None,
    )
    if opp is None:
        raise HTTPException(
            404,
            f"Pair {body.pair_key!r} direction {body.direction!r} not in current scan",
        )
    # Combined notional = qty × (kalshi_price + poly_price).
    combined_per_contract = (
        opp["kalshi_leg"]["price"] + opp["poly_leg"]["price"]
    )
    notional_dollars = combined_per_contract * body.contracts
    if not body.allow_over_cap and notional_dollars > MAX_ORDER_USD:
        raise HTTPException(
            400,
            f"Combined notional ${notional_dollars:.2f} exceeds MAX_ORDER_USD "
            f"(${MAX_ORDER_USD:.2f}). Re-send with allow_over_cap=true.",
        )

    token = uuid.uuid4().hex
    pending = {
        "token": token,
        "created_at": time.time(),
        "pair_key": body.pair_key,
        "direction": body.direction,
        "contracts": body.contracts,
        "opportunity": opp,
        "notional_dollars": notional_dollars,
        "over_cap": notional_dollars > MAX_ORDER_USD,
    }
    _PENDING[token] = pending
    return pending


# ---- /api/arb/execute -----------------------------------------------------

class ExecuteBody(BaseModel):
    token: str


def _log_order(record: dict) -> None:
    try:
        with ORDER_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        log.exception("failed to write order log")


@app.post("/api/arb/execute")
def api_arb_execute(body: ExecuteBody, _: str = Depends(require_auth)):
    pending = _PENDING.get(body.token)
    if not pending:
        raise HTTPException(400, "Unknown or already-used confirmation token")
    if time.time() - pending["created_at"] > 60:
        _PENDING.pop(body.token, None)
        raise HTTPException(400, "Token expired; re-quote and retry")
    _PENDING.pop(body.token, None)

    opp = pending["opportunity"]
    result = execute_arb(
        opp,
        contracts=pending["contracts"],
        kalshi_client=kalshi,
        poly_client=poly,
        dry_run=DRY_RUN,
    )
    record = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "dry_run": DRY_RUN,
        "intent": {
            "pair_key": pending["pair_key"],
            "direction": pending["direction"],
            "contracts": pending["contracts"],
            "notional_dollars": pending["notional_dollars"],
        },
        "result": result.as_dict(),
    }
    _log_order(record)

    # Live alert on every executed attempt (success or hedged).
    if NTFY_TOPIC and not DRY_RUN:
        title = f"EXEC {opp['label']} [{result.status}]"
        body_msg = (
            f"matched={result.matched_contracts:.0f} "
            f"realized=${(result.realized_pl or 0):.2f}"
        )
        ntfy_send(body_msg, title=title, priority="high",
                  tags=["white_check_mark" if result.status == "success" else "warning"])

    return {"ok": True, "dry_run": DRY_RUN, "result": result.as_dict()}
