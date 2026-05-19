"""
Trading client for Polymarket international (polymarket.com).

Polymarket.com runs on Polygon — orders are CLOB-style off-chain matching
backed by on-chain ERC-1155 conditional tokens (CTF). Trading requires:

  - A Polygon EOA private key (the signing key)
  - USDC.e balance on Polygon at the wallet address (or its proxy)
  - One-time allowances: USDC for the CTF Exchange contract, and CT
    allowance for selling (Polymarket app does this on first deposit)
  - Derived L2 API creds (api_key/secret/passphrase) — derived from the
    EOA key via create_or_derive_api_creds()

This wrapper:
  - Loads creds from env, derives L2 creds, instantiates ClobClient
  - Looks up token_id for a given (slug, outcome_index) via Gamma
  - Submits limit orders via create_and_post_order
  - Reports fill status via get_order

Wallet types:
  - POLY_PROXY (signature_type=1): Polymarket-issued proxy wallet, used
    when the user signed up via "Magic" email login
  - POLY_GNOSIS_SAFE (signature_type=2): Gnosis Safe proxy, used for
    mobile-app accounts
  - EOA (signature_type=0): direct EOA wallet — uncommon for app users

Set POLYMARKET_INTL_SIGNATURE_TYPE in env (default 2 = GNOSIS_SAFE,
which is what mobile app accounts use). The "funder" address (where
USDC lives) goes in POLYMARKET_INTL_FUNDER_ADDRESS.

Env vars:
  POLYMARKET_INTL_PRIVATE_KEY    Hex Polygon EOA private key (no 0x prefix OK)
  POLYMARKET_INTL_FUNDER_ADDRESS Proxy wallet address that holds USDC (0x...)
  POLYMARKET_INTL_SIGNATURE_TYPE 0 / 1 / 2 (default 2 for mobile app users)
  POLYMARKET_INTL_HOST           CLOB endpoint (default https://clob.polymarket.com)
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

log = logging.getLogger("polymarket_intl_trade")

POLYGON_CHAIN_ID = 137
DEFAULT_HOST = "https://clob.polymarket.com"


class PolymarketIntlTradeError(RuntimeError):
    pass


class PolymarketIntlNotConfigured(PolymarketIntlTradeError):
    pass


_GAMMA_BASE = "https://gamma-api.polymarket.com"


def _gamma_get(path: str, params: dict | None = None) -> Any:
    import urllib.parse
    url = _GAMMA_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "ArbScanner-IntlTrade",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _lookup_token_id(slug: str, outcome_index: int) -> tuple[str, dict]:
    """Resolve a polymarket.com slug + outcome index to a CLOB token_id.

    Returns (token_id, market_meta). Raises if the market or its
    clobTokenIds aren't populated.
    """
    res = _gamma_get("/markets", {"slug": slug})
    items = res if isinstance(res, list) else (res or {}).get("markets") or []
    if not items:
        raise PolymarketIntlTradeError(f"Gamma: market not found for slug={slug}")
    m = items[0]
    raw = m.get("clobTokenIds")
    try:
        token_ids = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except (json.JSONDecodeError, TypeError):
        token_ids = []
    if not token_ids or outcome_index >= len(token_ids):
        raise PolymarketIntlTradeError(
            f"slug {slug}: no clobTokenIds for outcome_index={outcome_index} "
            f"(have {len(token_ids)} ids)"
        )
    return str(token_ids[outcome_index]), m


class PolymarketIntlTradeClient:
    """Wrapper over py-clob-client for Polymarket.com CLOB trading.

    Lazy-imports the SDK so the rest of the system loads even if the
    SDK isn't installed (it's a heavy dep — web3, eth-account, etc.).
    """

    def __init__(self,
                 private_key: str | None = None,
                 funder_address: str | None = None,
                 signature_type: int | None = None,
                 host: str | None = None):
        self.private_key = private_key or os.getenv("POLYMARKET_INTL_PRIVATE_KEY", "").strip()
        self.funder_address = (funder_address
                               or os.getenv("POLYMARKET_INTL_FUNDER_ADDRESS", "")
                              ).strip()
        sig = signature_type if signature_type is not None else os.getenv(
            "POLYMARKET_INTL_SIGNATURE_TYPE", "2")
        try:
            self.signature_type = int(sig)
        except (ValueError, TypeError):
            self.signature_type = 2
        self.host = host or os.getenv("POLYMARKET_INTL_HOST", DEFAULT_HOST)
        self._client = None
        self._init_error: str | None = None
        if self.configured:
            self._try_init()

    @property
    def configured(self) -> bool:
        return bool(self.private_key and self.funder_address)

    def _try_init(self) -> None:
        """Lazy-import the SDK and instantiate. Stash the error if it
        fails so /api/debug/intl-trade can surface it without crashing
        the whole module load."""
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
        except ImportError as e:
            self._init_error = f"py-clob-client not installed: {e}"
            return
        try:
            pk = self.private_key
            if pk.startswith("0x"):
                pk = pk[2:]
            client = ClobClient(
                host=self.host,
                chain_id=POLYGON_CHAIN_ID,
                key=pk,
                signature_type=self.signature_type,
                funder=self.funder_address,
            )
            # Derive L2 creds (api_key/secret/passphrase) from the EOA key.
            # Required for authenticated endpoints — place_order etc.
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
            self._client = client
        except Exception as e:
            self._init_error = f"{type(e).__name__}: {e}"
            log.exception("intl trade init failed")

    def _require(self):
        if not self.configured:
            raise PolymarketIntlNotConfigured(
                "POLYMARKET_INTL_PRIVATE_KEY / "
                "POLYMARKET_INTL_FUNDER_ADDRESS not set"
            )
        if self._client is None:
            raise PolymarketIntlTradeError(
                f"intl client init failed: {self._init_error}"
            )

    def get_address(self) -> str:
        self._require()
        return self._client.get_address()

    def get_balance(self) -> dict:
        """Return the configured account's USDC balance + allowances."""
        self._require()
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        try:
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=self.signature_type,
            )
            return self._client.get_balance_allowance(params=params)
        except Exception as e:
            raise PolymarketIntlTradeError(f"get_balance_allowance: {e}") from e

    def place_order(self, *, slug: str, outcome_index: int,
                    side: str, size: float, price: float,
                    tif: str = "FAK") -> dict:
        """Place a limit order on polymarket.com CLOB.

        slug:           Polymarket market slug (e.g. mlb-tor-nyy-2026-05-20)
        outcome_index:  0 for the first outcome, 1 for the second
        side:           "BUY" or "SELL"
        size:           Number of shares
        price:          Limit price in probability (0.01 - 0.99)
        tif:            "FAK" (= IOC), "FOK", "GTC", "GTD". Default FAK so
                        unmatched portion cancels.
        """
        self._require()
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL
        token_id, market_meta = _lookup_token_id(slug, outcome_index)
        side_const = BUY if side.upper() == "BUY" else SELL
        order_args = OrderArgs(
            token_id=token_id,
            price=float(price),
            size=float(size),
            side=side_const,
        )
        order_type_enum = {
            "FAK": OrderType.FAK, "IOC": OrderType.FAK,
            "FOK": OrderType.FOK, "GTC": OrderType.GTC, "GTD": OrderType.GTD,
        }.get(tif.upper(), OrderType.FAK)
        try:
            resp = self._client.create_and_post_order(
                order_args,
                options=None,
            ) if False else self._client.create_and_post_order(order_args)
        except TypeError:
            # Some SDK versions split: build then post separately
            order = self._client.create_order(order_args)
            resp = self._client.post_order(order, orderType=order_type_enum)
        except Exception as e:
            raise PolymarketIntlTradeError(f"create_and_post_order: {e}") from e
        return {
            "raw": resp,
            "token_id": token_id,
            "market_slug": slug,
            "outcome": (market_meta.get("outcomes") or [None, None])[outcome_index]
                       if isinstance(market_meta.get("outcomes"), list)
                       else market_meta.get("outcomes"),
        }

    def get_order(self, order_id: str) -> dict:
        self._require()
        return self._client.get_order(order_id)

    def cancel(self, order_id: str) -> dict:
        self._require()
        return self._client.cancel(order_id)
