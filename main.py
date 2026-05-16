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

# ntfy.sh background push (works when the app is closed / phone locked)
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_BUY_EV_PCT = float(os.getenv("NTFY_BUY_EV_PCT", "1.5"))
NTFY_SELL_EDGE_C = float(os.getenv("NTFY_SELL_EDGE_C", "1.0"))
NTFY_COOLDOWN_SEC = int(os.getenv("NTFY_COOLDOWN_SEC", "300"))
NTFY_INTERVAL_SEC = int(os.getenv("NTFY_INTERVAL_SEC", "30"))

if not APP_PASSWORD:
    log.warning("APP_PASSWORD not set — app is OPEN. Do not deploy like this.")
if DRY_RUN:
    log.warning("DRY_RUN enabled — /api/order will NOT place real orders.")
if NTFY_TOPIC:
    log.info(f"ntfy push enabled: server={NTFY_SERVER}, interval={NTFY_INTERVAL_SEC}s, "
             f"buy>={NTFY_BUY_EV_PCT}%, sell>={NTFY_SELL_EDGE_C}c, cooldown={NTFY_COOLDOWN_SEC}s")
else:
    log.info("NTFY_TOPIC not set — background push disabled.")


# ---- ntfy push ------------------------------------------------------------

# Rising-edge state per key. Each entry:
#   {"above": bool — was the trigger met on the previous tick,
#    "last_fire_at": float — unix ts of last actual push}
# Pushes fire only on the False->True transition (plus a min refire gap
# as noise insurance). This kills the "same alert every cooldown" loop.
_ntfy_alert_state: dict[str, dict] = {}
_ntfy_stop = threading.Event()


def _ntfy_should_fire(key: str, condition_met: bool) -> bool:
    """Rising-edge with refire gap. Returns True iff we should push now."""
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
    """POST to ntfy.sh/{topic}. Returns (ok, error)."""
    if not NTFY_TOPIC:
        return False, "NTFY_TOPIC not set"
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    headers: dict[str, str] = {"Content-Type": "text/plain; charset=utf-8"}
    if title:
        # ntfy reads the Title header as Latin-1; encode unicode as ASCII-safe
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
    """One pass: scan, find +EV buy signals and net-of-fee sell signals on
    held positions, push novel ones via ntfy. Returns a small summary."""
    summary = {"buys_pushed": 0, "sells_pushed": 0, "errors": []}
    try:
        scan_data = scanner.run_scan(contracts=100, price_side="mid")
    except Exception as e:
        summary["errors"].append(f"scan: {e}")
        return summary

    # BUY signals — each row drives a rising-edge state machine.
    # We feed every (ticker,side) into _ntfy_should_fire so that the state
    # tracker also sees signals dropping BELOW threshold (resets the edge
    # so a future crossing can fire again).
    for r in scan_data.get("rows", []):
        ev_pct = (r.get("ev_per_dollar") or 0) * 100
        side = r.get("side", "yes")
        key = f"{r['ticker']}:{side}:buy"
        condition_met = ev_pct >= NTFY_BUY_EV_PCT
        if not _ntfy_should_fire(key, condition_met):
            continue
        title = f"BUY {side.upper()} {r['event_label']} {r['country']} +{ev_pct:.1f}% EV"
        body = (f"{side.upper()} ask {(r['kalshi_ask']*100):.0f}c -> Poly "
                f"P({side.upper()})={(r['true_prob']*100):.0f}c | {r['ticker']}")
        ok, err = ntfy_send(body, title=title, priority="high",
                            tags=["green_circle", "money"])
        if ok:
            summary["buys_pushed"] += 1
        else:
            summary["errors"].append(f"push buy {side} {r['ticker']}: {err}")

    # SELL signals — only if Kalshi creds are loaded. Sells close whatever
    # side the user actually holds; for a long-NO position we compare
    # no_bid vs (1 - true_prob_yes).
    if kalshi.configured:
        try:
            positions = kalshi.get_positions(limit=200)
        except (KalshiError, KalshiNotConfigured) as e:
            summary["errors"].append(f"positions: {e}")
            return summary
        price_by_ticker_side = {
            (r["ticker"], r["side"]): r for r in scan_data.get("rows", [])
        }
        raw = positions.get("market_positions") or positions.get("positions") or []
        for pos in raw:
            ticker = pos.get("ticker") or ""
            if "EUROVISION" not in ticker.upper():
                continue
            try:
                position_count = float(pos.get("position_fp")
                                       or pos.get("position") or 0)
            except (TypeError, ValueError):
                continue
            if position_count == 0:
                continue
            position_side = "yes" if position_count > 0 else "no"
            row = price_by_ticker_side.get((ticker, position_side), {})
            bid = row.get("kalshi_bid")
            fair = row.get("true_prob")
            if bid is None or fair is None or bid <= 0:
                continue
            fee = scanner.kalshi_taker_fee_per_contract(
                bid, int(abs(position_count)) or 100
            )
            net_edge = bid - fair - fee
            key = f"{ticker}:{position_side}:sell"
            condition_met = net_edge * 100 >= NTFY_SELL_EDGE_C
            if not _ntfy_should_fire(key, condition_met):
                continue
            ev_label, ev_country = _label_for_ticker(ticker)
            title = (f"SELL {position_side.upper()} {ev_label} "
                     f"{ev_country or ticker} +{net_edge*100:.1f}c NET")
            body = (f"{position_side.upper()} bid {(bid*100):.0f}c vs fair "
                    f"{(fair*100):.0f}c, after {(fee*100):.1f}c fee | "
                    f"{int(abs(position_count))} contracts | {ticker}")
            ok, err = ntfy_send(body, title=title, priority="high",
                                tags=["red_circle", "warning"])
            if ok:
                summary["sells_pushed"] += 1
            else:
                summary["errors"].append(f"push sell {position_side} {ticker}: {err}")
    return summary


def _ntfy_loop() -> None:
    log.info("ntfy background loop started")
    while not _ntfy_stop.is_set():
        try:
            s = _check_opportunities_and_push()
            if s["buys_pushed"] or s["sells_pushed"] or s["errors"]:
                log.info(f"ntfy loop: {s}")
        except Exception:
            log.exception("ntfy loop error")
        _ntfy_stop.wait(NTFY_INTERVAL_SEC)
    log.info("ntfy background loop stopped")


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


app = FastAPI(title="Eurovision EV Scanner", version="0.1", lifespan=_lifespan)
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
        "ntfy_configured": bool(NTFY_TOPIC),
    }


@app.post("/api/ntfy/test")
def api_ntfy_test(_: str = Depends(require_auth)):
    if not NTFY_TOPIC:
        raise HTTPException(503, "NTFY_TOPIC not set in env vars")
    ok, err = ntfy_send(
        "If you got this, server -> ntfy.sh -> your phone is wired up.",
        title="Eurovision EV: test push",
        priority="default",
        tags=["white_check_mark"],
    )
    if not ok:
        raise HTTPException(502, f"ntfy push failed: {err}")
    return {"ok": True, "server": NTFY_SERVER}


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
    # Lookup is keyed by (ticker, side) so YES vs NO holdings each get the
    # right bid/ask/fair-value reference.
    scan = scanner.run_scan(contracts=100)
    price_by_ticker_side = {
        (r["ticker"], r["side"]): r for r in scan.get("rows", [])
    }
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
        position_side = "yes" if position_count > 0 else "no"
        # Match the price row for the SIDE we actually hold — yes_bid/ask
        # for long YES, no_bid/ask for long NO. true_prob in the matched
        # row already reflects the held side's probability.
        row = price_by_ticker_side.get((ticker, position_side), {})

        exposure_dollars = _ff("market_exposure_dollars")
        avg_cost_cents = (exposure_dollars * 100 / abs(position_count)
                          if position_count else None)
        realized_pnl_dollars = _ff("realized_pnl_dollars")
        fees_paid_dollars = _ff("fees_paid_dollars")

        # Sell-side economics. Selling closes the held side at its bid:
        #   net edge = held_side_bid - true_prob_of_held_side - sell_fee
        # The bid/true_prob in `row` are already for the held side, so the
        # math is the same for YES and NO positions.
        sell_edge_net_per_contract = None
        sell_fee_per_contract = None
        if (row.get("kalshi_bid") is not None
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
            "side": position_side,
            "avg_cost_cents": avg_cost_cents,
            "exposure_dollars": exposure_dollars,
            "current_yes_bid": row.get("kalshi_bid"),   # bid of held side
            "current_yes_ask": row.get("kalshi_ask"),   # ask of held side
            "ev_per_dollar": row.get("ev_per_dollar"),  # EV of BUYING more of held side now
            "true_prob": row.get("true_prob"),          # P(held side wins)
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
    # Find the row for this exact (ticker, side) — kalshi_bid/ask in the
    # matched row are already the right side's prices, so the price math
    # is identical for YES and NO.
    row = next((r for r in scan["rows"]
                if r["ticker"] == body.ticker and r["side"] == body.side), None)
    if not row:
        raise HTTPException(
            404,
            f"Ticker {body.ticker} (side={body.side}) not in current scan"
        )

    if body.limit_price_cents is None:
        if body.action == "buy":
            px = int(round((row["kalshi_ask"] or 0.01) * 100))  # ask of this side
        else:  # sell
            px = int(round((row["kalshi_bid"] or 0.01) * 100))  # bid of this side
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
            limit_price_cents=pending["limit_price_cents"],
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
