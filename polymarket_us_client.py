"""
Thin wrapper around the official `polymarket-us` SDK that mirrors the
shape of kalshi_client.py: a `configured` property, normalized error
classes, and methods that return plain dicts.

Why a wrapper:
  - Scanner / executor stay venue-agnostic — both clients expose the
    same surface (get_orderbook, get_balance, get_positions, place_order).
  - Tests can monkey-patch one class instead of the SDK internals.
  - Handles the credential-missing case (public market data still works
    without auth; trading endpoints raise PolymarketUSNotConfigured).

The SDK does Ed25519 signing for us. Docs:
  https://docs.polymarket.us/api-reference/authentication
"""

from __future__ import annotations

import os
from typing import Any

try:
    from polymarket_us import PolymarketUS
    # The SDK's own PolymarketUSError is the base of every SDK exception.
    # We re-raise as our own class (same name, different module) so callers
    # only import from polymarket_us_client.
    from polymarket_us import PolymarketUSError as _SDKError
    _HAS_SDK = True
except ImportError:
    _HAS_SDK = False
    _SDKError = Exception  # type: ignore[misc,assignment]


class PolymarketUSError(RuntimeError):
    pass


class PolymarketUSNotConfigured(PolymarketUSError):
    """Raised when API credentials aren't set. Treat as 'trading unavailable'."""


def _wrap(fn):
    """Convert SDK exceptions to PolymarketUSError so callers only have to
    catch one error class."""
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _SDKError as e:
            raise PolymarketUSError(f"{type(e).__name__}: {e}") from e
    return inner


class PolymarketUSClient:
    """Wrapper exposing the subset of the SDK that scanner + executor use.

    Reads credentials from POLYMARKET_US_KEY_ID and POLYMARKET_US_SECRET_KEY
    by default. Public market data calls work without credentials; trading
    + portfolio calls raise PolymarketUSNotConfigured when creds are missing.
    """

    def __init__(self, key_id: str | None = None,
                 secret_key: str | None = None):
        if not _HAS_SDK:
            self._client = None
            self._configured = False
            return
        self.key_id = key_id or os.getenv("POLYMARKET_US_KEY_ID", "")
        self.secret_key = secret_key or os.getenv("POLYMARKET_US_SECRET_KEY", "")
        self._configured = bool(self.key_id and self.secret_key)
        if self._configured:
            self._client = PolymarketUS(
                key_id=self.key_id, secret_key=self.secret_key,
            )
        else:
            # SDK supports no-auth instantiation for public endpoints.
            self._client = PolymarketUS()

    @property
    def configured(self) -> bool:
        return self._configured

    def _require_auth(self) -> None:
        if not self._configured:
            raise PolymarketUSNotConfigured(
                "POLYMARKET_US_KEY_ID / POLYMARKET_US_SECRET_KEY not set"
            )

    # ---- public market data -------------------------------------------------

    @_wrap
    def get_market_meta(self, slug: str) -> dict:
        """Market metadata (slug, question, dates) — does NOT include live
        prices. Use get_quote for prices."""
        if not _HAS_SDK:
            raise PolymarketUSError("polymarket-us SDK not installed")
        return self._client.markets.retrieve_by_slug(slug)

    @_wrap
    def get_quote(self, slug: str) -> dict:
        """Top-of-book quote: bestBid, bestAsk, last trade, depths.

        Returns a flattened dict so callers don't have to dig through
        the SDK's `{marketData: {...}}` nesting:
            {
              "slug": str, "yes_bid": float|None, "yes_ask": float|None,
              "bid_depth": int, "ask_depth": int,
              "last_trade": float|None, "current_px": float|None,
              "open_interest": float, "shares_traded": float,
            }
        """
        if not _HAS_SDK:
            raise PolymarketUSError("polymarket-us SDK not installed")
        raw = self._client.markets.bbo(slug)
        md = (raw or {}).get("marketData") or {}
        def _amt(d):
            if isinstance(d, dict):
                try:
                    return float(d.get("value"))
                except (TypeError, ValueError):
                    return None
            return None
        return {
            "slug": md.get("marketSlug") or slug,
            "yes_bid": _amt(md.get("bestBid")),
            "yes_ask": _amt(md.get("bestAsk")),
            "bid_depth": md.get("bidDepth"),
            "ask_depth": md.get("askDepth"),
            "last_trade": _amt(md.get("lastTradePx")),
            "current_px": _amt(md.get("currentPx")),
            "open_interest": _f_local(md.get("openInterest"), 0.0),
            "shares_traded": _f_local(md.get("sharesTraded"), 0.0),
            "raw": raw,
        }

    @_wrap
    def get_orderbook(self, slug: str) -> dict:
        """Full order book with depth (price levels + sizes per side)."""
        if not _HAS_SDK:
            raise PolymarketUSError("polymarket-us SDK not installed")
        return self._client.markets.book(slug)


def _f_local(v, default):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default

    # ---- authenticated portfolio --------------------------------------------

    @_wrap
    def get_balance(self) -> dict:
        self._require_auth()
        return self._client.account.balances()

    @_wrap
    def get_positions(self) -> dict:
        self._require_auth()
        return self._client.portfolio.positions()

    # ---- authenticated trading ----------------------------------------------

    @_wrap
    def place_order(self, *, slug: str, side: str, action: str,
                    quantity: float, limit_price: float | None = None,
                    tif: str = "IOC", order_type: str = "limit") -> dict:
        """Place an order via the SDK.

        side:          'yes' or 'no'
        action:        'buy' or 'sell'
        quantity:      shares (float). Polymarket calls these "shares" but
                       semantically they're 1:1 with Kalshi contracts.
        limit_price:   probability 0-1 (e.g. 0.55 = 55c). Required for limit.
        tif:           'IOC' (default for arb legs), 'FOK', 'GTC', 'DAY', 'GTD'.
        order_type:    'limit' or 'market'.

        Translates to the SDK's enum constants under the hood. Returns the
        raw dict response — caller pulls out `id`, `executions`, etc.
        """
        self._require_auth()
        body: dict[str, Any] = {
            "marketSlug": slug,
            "type": _ORDER_TYPE_MAP[order_type],
            "tif": _TIF_MAP[tif],
            "outcomeSide": side.upper(),  # YES or NO
            "action": action.upper(),     # BUY or SELL
            "quantity": float(quantity),
        }
        if order_type == "limit":
            if limit_price is None:
                raise PolymarketUSError("limit_price required for limit orders")
            body["price"] = {
                "value": f"{float(limit_price):.4f}",
                "currency": "USD",
            }
        return self._client.orders.create(body)

    @_wrap
    def get_order(self, order_id: str) -> dict:
        self._require_auth()
        return self._client.orders.retrieve(order_id)

    @_wrap
    def cancel_order(self, order_id: str) -> dict:
        self._require_auth()
        return self._client.orders.cancel(order_id, {})


_ORDER_TYPE_MAP = {
    "limit":  "ORDER_TYPE_LIMIT",
    "market": "ORDER_TYPE_MARKET",
}

# `best_bid_ask` lived here previously to extract prices from the SDK's
# retrieve_by_slug response. That endpoint doesn't actually populate the
# price fields — use `get_quote(slug)` above instead, which calls bbo()
# and returns a flat dict.

# Polymarket US uses the enum names verbatim in request bodies.
_TIF_MAP = {
    "DAY": "TIME_IN_FORCE_DAY",
    "GTC": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
    "GTD": "TIME_IN_FORCE_GOOD_TILL_DATE",
    "IOC": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
    "FOK": "TIME_IN_FORCE_FILL_OR_KILL",
}


