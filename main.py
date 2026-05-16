"""
FastAPI app: live Kalshi/Polymarket Eurovision EV scanner + portfolio + order
placement.

DRY_RUN behavior:
    - If env var DRY_RUN is "1" / "true" / unset, /api/order returns a
      stubbed "Testing" response instead of hitting Kalshi.
    - When ready to go live, set DRY_RUN=false in Railway env vars and
      restart the service.

Env vars:
    APP_PASSWORD          shared password for HTTP Basic auth (required for
                          public deploy; if unset, app runs open in dev mode)
    KALSHI_KEY_ID         Kalshi API key ID
    KALSHI_PRIVATE_KEY    Kalshi private key PEM (with literal newlines OR
                          escaped \\n — both supported)
    DRY_RUN               "true" (default) = stub orders; "false" = live
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import scanner
from kalshi_client import KalshiClient, KalshiError, KalshiNotConfigured

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("eurovision")

APP_PASSWORD = os.getenv("APP_PASSWORD", "")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() not in ("0", "false", "no", "off")
MAX_ORDER_USD = float(os.getenv("MAX_ORDER_USD", "50"))

if not APP_PASSWORD:
    log.warning("APP_PASSWORD not set — app is OPEN. Do not deploy like this.")
if DRY_RUN:
    log.warning("DRY_RUN enabled — /api/order will NOT place real orders.")

app = FastAPI(title="Eurovision EV Scanner", version="0.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False,
    allow_methods=["*"], allow_headers=["*"],
)

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
ORDER_LOG_PATH = BASE_DIR / "orders.log.jsonl"

kalshi = KalshiClient()


def _label_for_ticker(ticker: str) -> tuple[str, str]:
    """Turn a Kalshi ticker into (event_label, country_name). country_name is
    "" if we can't decode it. Examples:
        KXEUROVISION-26-AUS         -> ("Winner",   "Australia")
        KXEUROVISIONJURY-26-FIN     -> ("Jury",     "Finland")
        KXEUROVISIONTELEVOTE-26-ISR -> ("Televote", "Israel")
        KXEUROVISIONTOP10-26-FIN    -> ("Top 10",   "Finland")
    Heuristic — adapt as we learn the real Kalshi series tickers.
    """
    parts = ticker.split("-")
    if not parts:
        return ("", "")
    series = parts[0].upper()
    suffix = parts[-1] if len(parts) >= 3 else ""
    country = scanner.TICKER_TO_COUNTRY.get(suffix, "")
    if series == "KXEUROVISION":
        return ("Winner", country)
    # Strip the "KXEUROVISION" prefix off and decode the rest.
    tail = series.replace("KXEUROVISION", "", 1)
    if tail.startswith("JURY"):
        return ("Jury", country)
    if tail.startswith("TELEV") or "TELEVOTE" in tail or "TELE" in tail:
        return ("Televote", country)
    if tail.startswith("TOP"):
        # e.g. TOP10, TOP5, TOP3
        return (f"Top {tail.replace('TOP', '')}".strip(), country)
    if "LAST" in tail:
        return ("Last Place", country)
    if "SEMI" in tail or tail.startswith("SF"):
        return ("Semi-Final", country)
    if "MARGIN" in tail:
        return ("Margin", country)
    if tail:
        return (tail.title(), country)
    return (series, country)

# In-memory cache so a single tap-through doesn't re-fetch unnecessarily.
_SCAN_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_PENDING_ORDERS: dict[str, dict] = {}  # confirmation tokens -> order details

security = HTTPBasic(auto_error=False)


def require_auth(creds: HTTPBasicCredentials | None = Depends(security)) -> str:
    if not APP_PASSWORD:
        return "dev"
    if creds is None or not secrets.compare_digest(creds.password or "", APP_PASSWORD):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Auth required",
            headers={"WWW-Authenticate": "Basic realm=\"Eurovision EV\""},
        )
    return creds.username or "user"


@app.get("/health")
def health():
    return {
        "ok": True,
        "dry_run": DRY_RUN,
        "kalshi_configured": kalshi.configured,
        "auth_enabled": bool(APP_PASSWORD),
    }


@app.get("/")
def index(_: str = Depends(require_auth)):
    return FileResponse(STATIC_DIR / "index.html")


# Serve static files (icons etc.) under /static
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---- /api/scan ------------------------------------------------------------

@app.get("/api/scan")
def api_scan(contracts: int = 100, price_side: str = "mid",
             _: str = Depends(require_auth)):
    # cache for 5s so a flurry of refreshes doesn't hammer upstreams
    now = time.time()
    cache_key = f"{contracts}:{price_side}"
    cached = _SCAN_CACHE.get(cache_key)
    if cached and now - cached["ts"] < 5.0:
        return cached["data"]
    data = scanner.run_scan(contracts=contracts, price_side=price_side)
    data["fetched_at"] = datetime.utcnow().isoformat() + "Z"
    _SCAN_CACHE[cache_key] = {"ts": now, "data": data}
    return data


# ---- /api/positions -------------------------------------------------------

@app.get("/api/positions")
def api_positions(_: str = Depends(require_auth)):
    if not kalshi.configured:
        return {
            "connected": False,
            "reason": "KALSHI_KEY_ID / KALSHI_PRIVATE_KEY not set",
            "positions": [],
            "balance": None,
        }
    try:
        balance = kalshi.get_balance()
        positions_resp = kalshi.get_positions(limit=200)
    except KalshiNotConfigured as e:
        return {"connected": False, "reason": str(e), "positions": [], "balance": None}
    except KalshiError as e:
        log.exception("kalshi positions fetch failed")
        return {"connected": False, "reason": str(e), "positions": [], "balance": None}

    # Enrich Kalshi positions with friendly label + current prices.
    # We show every Eurovision-adjacent position the user holds, not just
    # the main Winner market — they may also hold Jury / Televote / Top N
    # / Semi-Final / Last Place positions under sibling Kalshi series.
    scan = scanner.run_scan(contracts=100)
    price_by_ticker = {r["ticker"]: r for r in scan.get("rows", [])}
    raw_positions = (positions_resp.get("market_positions")
                     or positions_resp.get("positions") or [])
    enriched = []
    dropped_zero = 0
    dropped_non_eurovision = 0
    for pos in raw_positions:
        ticker = pos.get("ticker") or ""
        # Kalshi field names per official spec:
        #   position_fp           - signed fractional contract count (string)
        #   market_exposure_dollars - current $ notional (string, dollars)
        #   realized_pnl_dollars  - realized P&L this market (string, dollars)
        #   total_traded_dollars  - lifetime $ traded (string, dollars)
        #   fees_paid_dollars     - lifetime fees this market (string, dollars)
        # All "FixedPoint*" fields arrive as decimal strings like "100.5".
        def _ff(key: str) -> float:
            v = pos.get(key)
            if v is None or v == "":
                return 0.0
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        position_count = _ff("position_fp") or _ff("position")  # fallback for older shape
        if position_count == 0:
            dropped_zero += 1
            continue
        if "EUROVISION" not in ticker.upper():
            dropped_non_eurovision += 1
            continue

        label_event, label_country = _label_for_ticker(ticker)
        row = price_by_ticker.get(ticker, {})

        exposure_dollars = _ff("market_exposure_dollars")
        # avg cost per contract, expressed in cents to match the rest of the UI
        avg_cost_cents = (exposure_dollars * 100 / abs(position_count)
                          if position_count else None)
        realized_pnl_dollars = _ff("realized_pnl_dollars")
        fees_paid_dollars = _ff("fees_paid_dollars")

        # Sell-side economics (only meaningful for YES positions we hold).
        # If we sell at current bid, the net edge vs Polymarket fair value
        # is (bid - fair) - taker_fee_on_sell. Positive net = good to sell.
        sell_edge_net_per_contract = None
        sell_fee_per_contract = None
        if (position_count > 0
                and row.get("kalshi_bid") is not None
                and row.get("true_prob") is not None
                and row.get("kalshi_bid") > 0):
            sell_fee_per_contract = scanner.kalshi_taker_fee_per_contract(
                row["kalshi_bid"], int(abs(position_count)) or 100
            )
            sell_edge_net_per_contract = (
                row["kalshi_bid"] - row["true_prob"] - sell_fee_per_contract
            )

        enriched.append({
            "ticker": ticker,
            "country": label_country or ticker,
            "event_label": label_event,
            "position": position_count,
            "side": "yes" if position_count > 0 else "no",
            "avg_cost_cents": avg_cost_cents,
            "exposure_dollars": exposure_dollars,
            "current_yes_bid": row.get("kalshi_bid"),
            "current_yes_ask": row.get("kalshi_ask"),
            "ev_per_dollar": row.get("ev_per_dollar"),
            "true_prob": row.get("true_prob"),
            "sell_edge_net_per_contract": sell_edge_net_per_contract,
            "sell_fee_per_contract": sell_fee_per_contract,
            "realized_pnl_dollars": realized_pnl_dollars,
            "fees_paid_dollars": fees_paid_dollars,
            "raw": pos,
        })
    # Sort: biggest absolute position first
    enriched.sort(key=lambda p: abs(p["position"]), reverse=True)
    return {
        "connected": True,
        "balance_cents": balance.get("balance"),
        "balance_dollars": (balance.get("balance") or 0) / 100,
        "positions": enriched,
        "debug": {
            "kalshi_returned": len(raw_positions),
            "shown": len(enriched),
            "dropped_zero": dropped_zero,
            "dropped_non_eurovision": dropped_non_eurovision,
            "top_level_keys": sorted(positions_resp.keys()),
            "sample_position_keys": sorted(raw_positions[0].keys()) if raw_positions else [],
        },
    }


# ---- /api/recommend -------------------------------------------------------

class RecommendBody(BaseModel):
    ticker: str
    side: str = Field(pattern="^(yes|no)$")
    action: str = Field(pattern="^(buy|sell)$")
    count: int = Field(gt=0, le=10000)
    limit_price_cents: int | None = Field(default=None, ge=1, le=99)


@app.post("/api/recommend")
def api_recommend(body: RecommendBody, _: str = Depends(require_auth)):
    """Validate the proposed order and return a confirmation token.

    The frontend MUST get a token from here before calling /api/order.
    Token expires after 60s.
    """
    scan = scanner.run_scan(contracts=body.count)
    row = next((r for r in scan["rows"] if r["ticker"] == body.ticker), None)
    if not row:
        raise HTTPException(404, f"Ticker {body.ticker} not in current scan")

    # default to current ask for buy, current bid for sell
    if body.limit_price_cents is None:
        if body.action == "buy" and body.side == "yes":
            px = int(round(row["kalshi_ask"] * 100))
        elif body.action == "sell" and body.side == "yes":
            px = int(round(row["kalshi_bid"] * 100))
        elif body.action == "buy" and body.side == "no":
            px = int(round((1 - row["kalshi_bid"]) * 100))
        else:  # sell no
            px = int(round((1 - row["kalshi_ask"]) * 100))
        px = max(1, min(99, px))
    else:
        px = body.limit_price_cents

    notional_dollars = (px / 100) * body.count
    if notional_dollars > MAX_ORDER_USD:
        raise HTTPException(
            400,
            f"Order notional ${notional_dollars:.2f} exceeds MAX_ORDER_USD "
            f"(${MAX_ORDER_USD:.2f}). Reduce size or raise the limit."
        )

    token = uuid.uuid4().hex
    pending = {
        "token": token,
        "created_at": time.time(),
        "ticker": body.ticker,
        "country": row["country"],
        "side": body.side,
        "action": body.action,
        "count": body.count,
        "limit_price_cents": px,
        "notional_dollars": notional_dollars,
        "ev_per_dollar_at_quote": row["ev_per_dollar"],
        "true_prob_at_quote": row["true_prob"],
        "kalshi_ask_at_quote": row["kalshi_ask"],
        "fee_per_contract": row["fee_per_contract"],
    }
    _PENDING_ORDERS[token] = pending
    return pending


# ---- /api/order -----------------------------------------------------------

class OrderBody(BaseModel):
    token: str


def _log_order(record: dict) -> None:
    try:
        with ORDER_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        log.exception("failed to write order log")


@app.post("/api/order")
def api_order(body: OrderBody, _: str = Depends(require_auth)):
    pending = _PENDING_ORDERS.get(body.token)
    if not pending:
        raise HTTPException(400, "Unknown or already-used confirmation token")
    if time.time() - pending["created_at"] > 60:
        _PENDING_ORDERS.pop(body.token, None)
        raise HTTPException(400, "Confirmation token expired; re-quote and retry")
    # one-shot: consume the token immediately
    _PENDING_ORDERS.pop(body.token, None)

    base_record = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "dry_run": DRY_RUN,
        "intent": pending,
    }

    if DRY_RUN:
        record = {**base_record, "status": "dry_run", "message": "Testing"}
        _log_order(record)
        return {
            "ok": True,
            "dry_run": True,
            "message": "Testing",
            "would_have_sent": {
                "ticker": pending["ticker"],
                "side": pending["side"],
                "action": pending["action"],
                "count": pending["count"],
                "yes_price_cents": pending["limit_price_cents"],
            },
        }

    # ---- LIVE PATH (not reachable until DRY_RUN=false in env) ----
    if not kalshi.configured:
        raise HTTPException(503, "Kalshi credentials not configured")
    client_order_id = uuid.uuid4().hex
    try:
        resp = kalshi.place_order(
            ticker=pending["ticker"],
            side=pending["side"],
            action=pending["action"],
            count=pending["count"],
            yes_price_cents=pending["limit_price_cents"],
            client_order_id=client_order_id,
        )
    except KalshiError as e:
        record = {**base_record, "status": "error", "error": str(e)}
        _log_order(record)
        raise HTTPException(502, f"Kalshi rejected order: {e}")
    record = {**base_record, "status": "placed",
              "client_order_id": client_order_id, "kalshi_response": resp}
    _log_order(record)
    return {"ok": True, "dry_run": False, "kalshi": resp,
            "client_order_id": client_order_id}
