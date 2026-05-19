"""
Two-legged arbitrage executor.

Fires both legs of an arb opportunity concurrently with IOC time-in-force
so unmatched orders don't rest on the book. Handles the four outcomes:

    A. Both fully filled at matching size      -> success
    B. Both partially filled at matching size  -> success at reduced size
    C. Asymmetric fill                         -> market-hedge the imbalance
                                                  on the under-filled venue,
                                                  capped by MAX_HEDGE_SLIPPAGE_C
    D. Both unfilled                            -> no-op

DRY_RUN gate wraps every venue call. In dry-run, returns a synthetic
"filled at quoted price" result so the rest of the system can be tested
end-to-end without touching real money.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from kalshi_client import KalshiClient, KalshiError, KalshiNotConfigured
from polymarket_us_client import (
    PolymarketUSClient,
    PolymarketUSError,
    PolymarketUSNotConfigured,
)

log = logging.getLogger("arb_executor")

MAX_HEDGE_SLIPPAGE_C = float(os.getenv("MAX_HEDGE_SLIPPAGE_C", "2.0"))


# ---- result types ---------------------------------------------------------

@dataclass
class LegResult:
    venue: str
    market_id: str
    side: str
    action: str         # "buy" or "sell"
    requested_qty: int
    filled_qty: float = 0.0
    avg_fill_price: float | None = None
    order_id: str | None = None
    status: str = "pending"   # "filled", "partial", "rejected", "dry_run"
    error: str | None = None
    raw: dict | None = None

    def as_dict(self) -> dict:
        return {
            "venue": self.venue,
            "market_id": self.market_id,
            "side": self.side,
            "action": self.action,
            "requested_qty": self.requested_qty,
            "filled_qty": self.filled_qty,
            "avg_fill_price": self.avg_fill_price,
            "order_id": self.order_id,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class ArbResult:
    pair_key: str
    direction: str
    requested_contracts: int
    matched_contracts: float = 0.0
    locked_spread_per_contract: float | None = None
    realized_pl: float | None = None
    kalshi_leg: LegResult | None = None
    poly_leg: LegResult | None = None
    hedge_leg: LegResult | None = None
    unwind_leg: LegResult | None = None
    naked_exposure: dict | None = None  # populated if user is STILL unhedged after unwind attempt
    status: str = "pending"   # success, partial_success, hedged, unwound, naked, failed, dry_run
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "pair_key": self.pair_key,
            "direction": self.direction,
            "requested_contracts": self.requested_contracts,
            "matched_contracts": self.matched_contracts,
            "locked_spread_per_contract": self.locked_spread_per_contract,
            "realized_pl": self.realized_pl,
            "kalshi_leg": self.kalshi_leg.as_dict() if self.kalshi_leg else None,
            "poly_leg":   self.poly_leg.as_dict()   if self.poly_leg   else None,
            "hedge_leg":  self.hedge_leg.as_dict()  if self.hedge_leg  else None,
            "unwind_leg": self.unwind_leg.as_dict() if self.unwind_leg else None,
            "naked_exposure": self.naked_exposure,
            "status": self.status,
            "notes": self.notes,
        }


# ---- order placement helpers ---------------------------------------------

def _place_kalshi(kc: KalshiClient, leg: dict, contracts: int,
                  tif: str = "immediate_or_cancel",
                  action: str = "buy",
                  dry_run: bool = True) -> LegResult:
    res = LegResult(
        venue="kalshi",
        market_id=leg["market_id"],
        side=leg["side"],
        action=action,
        requested_qty=contracts,
    )
    limit_cents = max(1, min(99, int(round(leg["price"] * 100))))
    if dry_run:
        res.status = "dry_run"
        res.filled_qty = float(contracts)
        res.avg_fill_price = limit_cents / 100.0
        log.info(
            "[DRY_RUN] kalshi: would place %s %s %s@%dc tif=%s",
            action.upper(), leg["side"], contracts, limit_cents, tif
        )
        res.raw = {"dry_run": True, "limit_cents": limit_cents, "tif": tif, "action": action}
        return res
    if not kc.configured:
        res.status = "rejected"
        res.error = "Kalshi not configured"
        return res
    client_order_id = uuid.uuid4().hex
    try:
        resp = kc.place_order(
            ticker=leg["market_id"],
            side=leg["side"],
            action=action,
            count=contracts,
            limit_price_cents=limit_cents,
            client_order_id=client_order_id,
            time_in_force=tif,
        )
    except KalshiError as e:
        res.status = "rejected"
        res.error = str(e)
        return res
    res.raw = resp
    order = resp.get("order") or {}
    res.order_id = order.get("order_id") or client_order_id
    # Kalshi response includes the order's current state — try a few field
    # name candidates, since the API has evolved.
    res.filled_qty = float(order.get("taker_fill_count")
                           or order.get("filled_count")
                           or order.get("filled_qty") or 0)
    res.status = "filled" if res.filled_qty >= contracts else (
        "partial" if res.filled_qty > 0 else "rejected"
    )
    if res.filled_qty > 0:
        fill_cost_cents = (order.get("taker_fill_cost_cents")
                           or order.get("filled_cost_cents"))
        if fill_cost_cents:
            res.avg_fill_price = float(fill_cost_cents) / 100.0 / res.filled_qty
        else:
            res.avg_fill_price = limit_cents / 100.0
    return res


def _place_poly(pc: PolymarketUSClient, leg: dict, contracts: int,
                tif: str = "IOC",
                action: str = "buy",
                dry_run: bool = True) -> LegResult:
    res = LegResult(
        venue="polymarket_us",
        market_id=leg["market_id"],
        side=leg["side"],
        action=action,
        requested_qty=contracts,
    )
    if dry_run:
        res.status = "dry_run"
        res.filled_qty = float(contracts)
        res.avg_fill_price = float(leg["price"])
        log.info(
            "[DRY_RUN] polymarket: would place %s %s %s shares @ %.4f tif=%s",
            action.upper(), leg["side"], contracts, leg["price"], tif
        )
        res.raw = {"dry_run": True, "limit_price": leg["price"], "tif": tif, "action": action}
        return res
    if not pc.configured:
        res.status = "rejected"
        res.error = "Polymarket US not configured"
        return res
    try:
        resp = pc.place_order(
            slug=leg["market_id"],
            side=leg["side"],
            action=action,
            quantity=float(contracts),
            limit_price=float(leg["price"]),
            tif=tif,
            order_type="limit",
        )
    except PolymarketUSError as e:
        res.status = "rejected"
        res.error = str(e)
        return res
    res.raw = resp
    res.order_id = resp.get("id")
    # Polymarket returns executions[] when synchronousExecution was requested.
    # The IOC TIF causes the unfilled portion to cancel immediately, so we
    # can read filled size from executions or by re-fetching the order.
    executions = resp.get("executions") or []
    filled = sum(float(e.get("lastShares") or 0) for e in executions)
    if filled == 0:
        # IOC: nothing matched.
        res.status = "rejected"
        res.filled_qty = 0
        return res
    res.filled_qty = filled
    notional = sum(float(e.get("lastShares") or 0) * float(e.get("lastPx") or 0)
                   for e in executions)
    res.avg_fill_price = (notional / filled) if filled else None
    res.status = "filled" if filled >= contracts else "partial"
    return res


# ---- threaded concurrent firing -----------------------------------------

def _fire_both_legs(kc: KalshiClient, pc: PolymarketUSClient,
                    opp: dict, contracts: int,
                    dry_run: bool) -> tuple[LegResult, LegResult]:
    """Fire both legs concurrently via threads. Return (kalshi_result,
    poly_result). Threads are used (not asyncio) because both SDKs are
    sync."""
    results: dict[str, LegResult] = {}

    def _k():
        results["k"] = _place_kalshi(kc, opp["kalshi_leg"], contracts,
                                     dry_run=dry_run)

    def _p():
        results["p"] = _place_poly(pc, opp["poly_leg"], contracts,
                                   dry_run=dry_run)

    tk = threading.Thread(target=_k, daemon=True)
    tp = threading.Thread(target=_p, daemon=True)
    tk.start(); tp.start()
    tk.join(); tp.join()
    return results["k"], results["p"]


# ---- hedging --------------------------------------------------------------

def _hedge_imbalance(kc: KalshiClient, pc: PolymarketUSClient,
                     opp: dict, k_res: LegResult, p_res: LegResult,
                     dry_run: bool) -> LegResult | None:
    """If one leg over-filled relative to the other, buy more of the
    under-filled side at market (up to MAX_HEDGE_SLIPPAGE_C of slippage)
    to restore the matched-pair invariant.

    Returns the hedge LegResult, or None if no hedge was needed/possible.
    """
    matched = min(k_res.filled_qty, p_res.filled_qty)
    short_kalshi = p_res.filled_qty - matched
    short_poly   = k_res.filled_qty - matched
    if short_kalshi == 0 and short_poly == 0:
        return None

    if short_kalshi > 0:
        # Need MORE Kalshi contracts. Buy at ask + slippage cap.
        target_leg = opp["kalshi_leg"]
        max_price = min(1.0, target_leg["price"] + MAX_HEDGE_SLIPPAGE_C / 100.0)
        hedge_leg = {
            **target_leg,
            "price": max_price,
        }
        return _place_kalshi(kc, hedge_leg, int(short_kalshi), dry_run=dry_run)

    # short_poly > 0: need MORE Polymarket shares.
    target_leg = opp["poly_leg"]
    max_price = min(1.0, target_leg["price"] + MAX_HEDGE_SLIPPAGE_C / 100.0)
    hedge_leg = {
        **target_leg,
        "price": max_price,
    }
    return _place_poly(pc, hedge_leg, int(short_poly), dry_run=dry_run)


def _unwind_overfilled_leg(kc: KalshiClient, pc: PolymarketUSClient,
                           opp: dict, k_res: LegResult, p_res: LegResult,
                           dry_run: bool) -> LegResult | None:
    """LAST-RESORT fallback when the hedge attempt also fails: sell back
    the over-filled leg to flat instead of leaving the user with a naked
    directional position.

    This eats whatever bid-ask spread + fees the over-filled venue has,
    but it's strictly better than carrying open directional risk because
    the other side won't trade.

    Returns the unwind LegResult, or None if no unwind was needed.
    """
    matched = min(k_res.filled_qty, p_res.filled_qty)
    over_kalshi = k_res.filled_qty - matched
    over_poly   = p_res.filled_qty - matched
    if over_kalshi == 0 and over_poly == 0:
        return None

    if over_kalshi > 0:
        # We have extra Kalshi contracts. Sell them at the bid (or 2c
        # below the original ask as a fallback if bid is missing) with
        # IOC so we don't leave an order resting.
        target = opp["kalshi_leg"]
        bid = target.get("bid")
        if not bid or bid <= 0:
            bid = max(0.02, target["price"] - 0.02)
        sell_leg = {**target, "price": bid}
        log.warning(
            "Hedge failed — unwinding %s extra Kalshi %s contracts at %.2fc bid",
            int(over_kalshi), target["side"], bid * 100
        )
        return _place_kalshi(kc, sell_leg, int(over_kalshi),
                             action="sell", dry_run=dry_run)

    # over_poly > 0
    target = opp["poly_leg"]
    bid = target.get("bid")
    if not bid or bid <= 0:
        bid = max(0.02, target["price"] - 0.02)
    sell_leg = {**target, "price": bid}
    log.warning(
        "Hedge failed — unwinding %s extra Polymarket %s shares at %.4f bid",
        int(over_poly), target["side"], bid
    )
    return _place_poly(pc, sell_leg, int(over_poly),
                       action="sell", dry_run=dry_run)


# ---- public entry --------------------------------------------------------

def execute_arb(opp: dict, contracts: int, *,
                kalshi_client: KalshiClient | None = None,
                poly_client: PolymarketUSClient | None = None,
                dry_run: bool = True) -> ArbResult:
    """Execute a two-leg arb opportunity.

    opp: a row from scanner.run_scan(). Must contain pair_key, direction,
         kalshi_leg, poly_leg, cost_per_contract.
    contracts: number of contract pairs to attempt (one Kalshi contract +
         one Polymarket share per pair).
    """
    kc = kalshi_client or KalshiClient()
    pc = poly_client or PolymarketUSClient()

    result = ArbResult(
        pair_key=opp["pair_key"],
        direction=opp["direction"],
        requested_contracts=contracts,
    )

    # Intl-venue pairs: Polymarket leg lives on polymarket.com (Polygon,
    # USDC, EIP-712 CLOB) — we don't have a trading integration there.
    # Refuse to auto-execute. Surface a manual-execution payload with
    # the exact orders for the user to place on each side.
    if opp.get("polymarket_venue") == "intl":
        kalshi_price_cents = round(opp["kalshi_leg"]["price"] * 100, 1)
        poly_price = opp["poly_leg"]["price"]
        poly_side_label = ("YES (= " + str(opp["poly_leg"].get("outcomes", [""])[0]) + ")"
                          if "outcomes" in opp["poly_leg"]
                          else opp["poly_leg"]["side"].upper())
        result.status = "manual_required"
        result.notes.append(
            f"INTL pair — execute manually on BOTH venues. "
            f"On Kalshi: BUY {opp['kalshi_leg']['side'].upper()} "
            f"{contracts} contracts of {opp['kalshi_leg']['market_id']} "
            f"@ {kalshi_price_cents}c. "
            f"On Polymarket.com: open {opp['poly_leg']['market_id']} and BUY "
            f"{opp['poly_leg']['side'].upper()} {contracts} shares @ "
            f"{poly_price:.3f}."
        )
        result.notes.append(
            "Auto-execute is disabled on intl pairs because we don't have "
            "a Polygon CLOB integration. Place both legs as close in time "
            "as possible so prices don't move."
        )
        result.naked_exposure = None
        return result

    # Fire both legs concurrently.
    k_res, p_res = _fire_both_legs(kc, pc, opp, contracts, dry_run)
    result.kalshi_leg = k_res
    result.poly_leg = p_res

    # Step 1: try to hedge any imbalance by buying more of the
    # under-filled side at a slightly worse price.
    hedge = _hedge_imbalance(kc, pc, opp, k_res, p_res, dry_run)
    if hedge is not None:
        result.hedge_leg = hedge
        if hedge.venue == "kalshi":
            k_res.filled_qty += hedge.filled_qty
        else:
            p_res.filled_qty += hedge.filled_qty

    # Step 2 (NEW): if the hedge failed to fully close the imbalance,
    # the user now has unhedged directional exposure on the over-filled
    # venue. Sell that excess back to flat — eats the bid-ask spread but
    # strictly better than carrying naked risk.
    unwind = _unwind_overfilled_leg(kc, pc, opp, k_res, p_res, dry_run)
    if unwind is not None:
        result.unwind_leg = unwind
        # The unwind REDUCES filled_qty on its venue:
        if unwind.venue == "kalshi":
            k_res.filled_qty -= unwind.filled_qty
        else:
            p_res.filled_qty -= unwind.filled_qty

    matched = min(k_res.filled_qty, p_res.filled_qty)
    result.matched_contracts = matched

    # Status + P&L. Locked spread is per-contract; total P&L = matched ×
    # locked spread (minus any extra slippage from hedging at worse-than-
    # quoted prices, which is implicit in the realized fills).
    quoted_cost = opp["cost_per_contract"]
    if matched > 0:
        actual_cost_per_contract = (
            (k_res.avg_fill_price or opp["kalshi_leg"]["price"])
            + (p_res.avg_fill_price or opp["poly_leg"]["price"])
            + opp["kalshi_leg"]["fee"]
            + opp["poly_leg"]["fee"]
        )
        # If hedging happened, fold in the slippage cost vs the quoted leg.
        if hedge and hedge.avg_fill_price is not None:
            quoted_leg_price = (
                opp["kalshi_leg"]["price"] if hedge.venue == "kalshi"
                else opp["poly_leg"]["price"]
            )
            slip = max(0.0, hedge.avg_fill_price - quoted_leg_price)
            actual_cost_per_contract += slip * hedge.filled_qty / matched
        result.locked_spread_per_contract = 1.0 - actual_cost_per_contract
        result.realized_pl = result.locked_spread_per_contract * matched

    # Final naked-exposure check: are the two sides still imbalanced after
    # hedge + unwind? If so, the user has open directional risk and must
    # intervene manually.
    final_imbalance = abs(k_res.filled_qty - p_res.filled_qty)
    if final_imbalance > 0 and not dry_run:
        over = "kalshi" if k_res.filled_qty > p_res.filled_qty else "polymarket_us"
        result.naked_exposure = {
            "venue": over,
            "shares": float(final_imbalance),
            "side": (k_res.side if over == "kalshi" else p_res.side),
            "market_id": (k_res.market_id if over == "kalshi" else p_res.market_id),
            "instruction": (
                f"Manually close: SELL {int(final_imbalance)} {('YES' if (k_res.side if over=='kalshi' else p_res.side)=='yes' else 'NO')} "
                f"on {over} for market {k_res.market_id if over=='kalshi' else p_res.market_id}, "
                f"OR buy the opposite leg on the other venue to complete the arb."
            ),
        }

    if dry_run:
        result.status = "dry_run"
    elif final_imbalance > 0:
        result.status = "naked"
        result.notes.append(
            f"!!! UNHEDGED EXPOSURE: {int(final_imbalance)} extra "
            f"{result.naked_exposure['venue']} {result.naked_exposure['side']} "
            f"contracts. Both hedge and unwind attempts failed. "
            f"Close manually NOW."
        )
    elif unwind is not None and unwind.filled_qty > 0:
        result.status = "unwound"
        result.notes.append(
            f"Hedge failed; unwound {int(unwind.filled_qty)} excess "
            f"{unwind.venue} contracts at the bid (ate the bid-ask spread). "
            f"No naked exposure remains."
        )
    elif matched == 0:
        result.status = "failed"
        result.notes.append("Neither leg filled.")
    elif hedge is not None and hedge.filled_qty > 0:
        result.status = "hedged"
        result.notes.append(
            f"Imbalance hedged with {int(hedge.filled_qty)} extra "
            f"{hedge.venue} contracts."
        )
    elif matched >= contracts:
        result.status = "success"
    else:
        result.status = "partial_success"
        result.notes.append(
            f"Both legs partially filled at {int(matched)}/{contracts} contracts."
        )
    return result
