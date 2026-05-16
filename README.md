# Eurovision EV — Kalshi vs Polymarket Scanner

Mobile-first web app that finds +EV opportunities on Kalshi's Eurovision
markets by comparing against Polymarket prices, lets you view your live
Kalshi positions, and (when enabled) executes orders.

## Status

Phase 1: **DRY-RUN**. The order endpoint is wired end-to-end but the
actual Kalshi `POST /portfolio/orders` call is short-circuited and the
UI shows "Testing" instead. Flip `DRY_RUN=false` in Railway env vars when
ready to go live.

## Stack

- Python 3.12 + FastAPI + uvicorn
- Pure stdlib HTTP for upstream calls (urllib)
- `cryptography` for Kalshi RSA-PSS request signing
- Vanilla HTML/JS frontend, Tailwind via CDN (no build step)

## Endpoints

- `GET /` — mobile UI (HTTP Basic auth if `APP_PASSWORD` set)
- `GET /health` — diagnostic; reports `dry_run`, `kalshi_configured`, `auth_enabled`
- `GET /api/scan?contracts=100&price_side=mid` — live EV scan
- `GET /api/positions` — your Kalshi balance + Eurovision positions
- `POST /api/recommend` — validate a proposed order, return a confirmation token
- `POST /api/order` — execute the token (DRY-RUN by default)

## Environment variables

| Var | Required | Notes |
|---|---|---|
| `APP_PASSWORD` | yes for deploy | HTTP Basic password gate. Any username works. |
| `KALSHI_KEY_ID` | for /api/positions and live orders | UUID-style Key ID from Kalshi → API Keys |
| `KALSHI_PRIVATE_KEY` | for /api/positions and live orders | Full PEM contents. Newlines can be literal `\n` (Railway pastes work). |
| `DRY_RUN` | no | `true` (default) = stub orders; `false` = real |
| `MAX_ORDER_USD` | no | Per-order notional cap. Default 50. |

## Local dev

```bash
pip install -r requirements.txt
# Optional: set creds (or skip — positions will show "not connected")
export KALSHI_KEY_ID=...
export KALSHI_PRIVATE_KEY="$(cat path/to/kalshi_private_key.pem)"
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000 — no `APP_PASSWORD` set means no auth in dev.

## Railway deploy

1. Push this folder to GitHub.
2. New Railway project → "Deploy from GitHub repo" → pick the repo.
3. Variables tab → set `APP_PASSWORD`, `KALSHI_KEY_ID`, `KALSHI_PRIVATE_KEY`.
   Leave `DRY_RUN=true` for now.
4. Settings → Generate Domain. Open on your phone.
5. iPhone: Safari → Share → Add to Home Screen.

## CLI (for testing without the web app)

```bash
python eurovision_ev.py                     # one-shot scan
python eurovision_ev.py --loop 20           # refresh every 20s
python eurovision_ev.py --contracts 50      # size the fee calc
```

## Safety notes

- Orders require a confirmation token from `/api/recommend` that expires
  in 60s — you can't accidentally fire by hitting a URL.
- Every order attempt (dry-run or live) is appended to `orders.log.jsonl`.
- `MAX_ORDER_USD` caps per-order notional. Keep it small while testing.
- The private key never leaves your Railway env. Don't paste it into chat
  or commit it.
