"""
Authenticated Kalshi API client.

Auth scheme: each request signed with RSA-PSS SHA-256 over
    {timestamp_ms}{HTTP_METHOD}{path_no_query}
Signature is base64-encoded and sent in KALSHI-ACCESS-SIGNATURE.

Docs: https://docs.kalshi.com/getting_started/api_keys
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


class KalshiError(RuntimeError):
    pass


class KalshiNotConfigured(KalshiError):
    """Raised when API credentials aren't set. Treat as 'feature unavailable'."""


def _load_private_key(pem_data: str):
    if not _HAS_CRYPTO:
        raise KalshiError("cryptography package not installed")
    if not pem_data:
        raise KalshiNotConfigured("no private key configured")
    # Allow either raw PEM or PEM with literal "\n" escapes (common in env vars)
    if "\\n" in pem_data and "-----BEGIN" in pem_data:
        pem_data = pem_data.replace("\\n", "\n")
    return serialization.load_pem_private_key(
        pem_data.encode("utf-8"), password=None
    )


class KalshiClient:
    def __init__(self, key_id: str | None = None,
                 private_key_pem: str | None = None,
                 private_key_path: str | None = None):
        self.key_id = key_id or os.getenv("KALSHI_KEY_ID", "")
        pem = private_key_pem or os.getenv("KALSHI_PRIVATE_KEY", "")
        if not pem and (private_key_path or os.getenv("KALSHI_PRIVATE_KEY_PATH")):
            path = Path(private_key_path or os.getenv("KALSHI_PRIVATE_KEY_PATH"))
            if path.exists():
                pem = path.read_text(encoding="utf-8")
        if not self.key_id or not pem:
            self._key = None
            return
        self._key = _load_private_key(pem)

    @property
    def configured(self) -> bool:
        return self._key is not None and bool(self.key_id)

    def _sign(self, method: str, path: str) -> tuple[str, str]:
        if not self.configured:
            raise KalshiNotConfigured("Kalshi credentials missing")
        timestamp_ms = str(int(time.time() * 1000))
        msg = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")
        signature = self._key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return timestamp_ms, base64.b64encode(signature).decode("ascii")

    def _request(self, method: str, path: str,
                 params: dict | None = None,
                 body: dict | None = None,
                 timeout: float = 15.0) -> Any:
        if not self.configured:
            raise KalshiNotConfigured("Kalshi credentials missing")
        # Sign path WITHOUT query string.
        timestamp_ms, signature = self._sign(method, path)
        url = f"{API_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "Accept": "application/json",
            "User-Agent": _BROWSER_UA,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if not raw:
                    return {}
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise KalshiError(f"{method} {path} -> HTTP {e.code}: {err_body}") from e
        except urllib.error.URLError as e:
            raise KalshiError(f"{method} {path} -> network error: {e}") from e

    # ---- portfolio endpoints ----------------------------------------------

    def get_balance(self) -> dict:
        return self._request("GET", "/portfolio/balance")

    def get_positions(self, limit: int = 200) -> dict:
        return self._request("GET", "/portfolio/positions",
                             params={"limit": limit})

    def get_orders(self, status: str = "resting", limit: int = 200) -> dict:
        return self._request("GET", "/portfolio/orders",
                             params={"status": status, "limit": limit})

    def get_fills(self, limit: int = 100) -> dict:
        return self._request("GET", "/portfolio/fills",
                             params={"limit": limit})

    def place_order(self, ticker: str, side: str, action: str,
                    count: int, yes_price_cents: int,
                    client_order_id: str,
                    order_type: str = "limit",
                    time_in_force: str = "GTC") -> dict:
        """Place an order.

        side: 'yes' or 'no'
        action: 'buy' or 'sell'
        count: number of contracts
        yes_price_cents: integer price in cents (1-99) for the YES side
        client_order_id: idempotency token; passing the same value twice
            with the same intent will not double-fire.
        """
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": side,
            "action": action,
            "count": int(count),
            "type": order_type,
            "yes_price": int(yes_price_cents),
        }
        return self._request("POST", "/portfolio/orders", body=body)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/portfolio/orders/{order_id}")
