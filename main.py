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


# ---- app lifecycle --------------------------------------------------------

@asynccontextmanager
async def _lifespan(_: FastAPI):
    thread = None
    if NTFY_TOPIC:
        thread = threading.Thread(target=_ntfy_loop, daemon=True, name="ntfy")
        thread.start()
    try:
        yield
    finally:
        _ntfy_stop.set()


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
def api_scan(contracts: int = 100, min_spread: float = -5.0,
             _: str = Depends(require_auth)):
    """Return all registered pairs with their current arb spread.

    Default min_spread=-5.0c shows near-misses too so the UI has visibility
    into pairs that aren't currently arb-able. Pass min_spread=0 to filter
    to positive arbs only.
    """
    now = time.time()
    cache_key = f"{contracts}:{min_spread}"
    cached = _SCAN_CACHE.get(cache_key)
    if cached and now - cached["ts"] < 5.0:
        return cached["data"]
    data = scanner.run_scan(contracts=contracts, min_spread_cents=min_spread)
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
    """Find the Polymarket position dict for one slug. The endpoint
    documentation describes positions as a map of market slug -> position;
    we accept either dict or list shape to be robust."""
    if isinstance(positions_resp, dict):
        # Map shape: {slug: position_dict}
        if slug in positions_resp and isinstance(positions_resp[slug], dict):
            return positions_resp[slug]
        # Nested under "positions" key
        nested = positions_resp.get("positions")
        if isinstance(nested, dict) and slug in nested:
            return nested[slug]
        if isinstance(nested, list):
            for p in nested:
                if (p.get("marketSlug") or p.get("slug") or "").lower() == slug.lower():
                    return p
    return {}


@app.get("/api/positions")
def api_positions(_: str = Depends(require_auth)):
    """Per-pair view: for each registered pair, fetch holdings on both
    venues and report the combined position."""
    kalshi_balance: dict = {}
    kalshi_positions: dict = {}
    poly_balances: dict = {}
    poly_positions: dict = {}
    errors: list[str] = []

    if kalshi.configured:
        try:
            kalshi_balance = kalshi.get_balance()
            kalshi_positions = kalshi.get_positions(limit=200)
        except (KalshiError, KalshiNotConfigured) as e:
            errors.append(f"kalshi: {e}")

    if poly.configured:
        try:
            poly_balances = poly.get_balance()
        except (PolymarketUSError, PolymarketUSNotConfigured) as e:
            errors.append(f"polymarket balance: {e}")
        try:
            poly_positions = poly.get_positions()
        except (PolymarketUSError, PolymarketUSNotConfigured) as e:
            errors.append(f"polymarket positions: {e}")

    pairs = scanner.load_registry()
    rows = []
    for cfg in pairs:
        kpos = _kalshi_position_for_ticker(kalshi_positions, cfg.kalshi_ticker)
        ppos = _poly_position_for_slug(poly_positions, cfg.polymarket_us_slug)
        kshares = _f(kpos.get("position_fp") or kpos.get("position"))
        kside = "yes" if kshares > 0 else ("no" if kshares < 0 else None)
        # Polymarket positions: net quantity field varies; try several names.
        pshares = _f(ppos.get("netQuantity") or ppos.get("netQty")
                     or ppos.get("quantity") or ppos.get("position"))
        # Polymarket per-position outcome side: same field-name dance.
        pside = (ppos.get("outcomeSide") or ppos.get("side") or "").lower() or None
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
                "raw": kpos or None,
            },
            "polymarket": {
                "shares": pshares,
                "side": pside,
                "cost_basis": _f(ppos.get("costBasis") or ppos.get("cost_basis")),
                "realized_pnl": _f(ppos.get("realizedPnl") or ppos.get("realized_pnl")),
                "raw": ppos or None,
            },
        })

    # Kalshi balance: returned in cents.
    cash_k = (kalshi_balance.get("balance") or 0) / 100.0
    pv_k = (kalshi_balance.get("portfolio_value") or 0) / 100.0
    # Polymarket balances: dict response, structure TBD; surface the raw.
    poly_summary = {
        "raw": poly_balances or None,
    }
    return {
        "kalshi_connected": kalshi.configured,
        "polymarket_us_connected": poly.configured,
        "kalshi_cash_dollars": cash_k,
        "kalshi_portfolio_value_dollars": pv_k,
        "polymarket_us": poly_summary,
        "pairs": rows,
        "errors": errors,
    }


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
