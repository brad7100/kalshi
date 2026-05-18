# Arb Scanner — Kalshi ↔ Polymarket US

Mobile-first web app + CLI that scans curated pairs of Kalshi and
Polymarket US markets for **true cross-platform arbitrage** — pairs where
buying YES on one venue and NO on the other locks in profit after fees —
and (when enabled) auto-executes both legs.

## Arb math

For each registered pair, the scanner evaluates both directions:

```
Direction A:  BUY YES @ Kalshi  +  BUY NO @ Polymarket US
              cost   = kalshi_yes_ask + poly_no_ask
                       + kalshi_taker_fee + poly_taker_fee
              locked = $1 − cost     (per matched contract pair)

Direction B:  BUY YES @ Polymarket US  +  BUY NO @ Kalshi
              (mirror)
```

Rows with `locked > 0` are real arbs — every matched pair pays out $1 at
settlement regardless of outcome. On Polymarket, NO prices are derived
as the complement of YES (`no_ask = 1 − yes_bid`).

**Fees**
- Kalshi taker: `ceil(0.07 × N × P × (1 − P) × 100) / 100`, divided by N.
- Polymarket US taker: `POLY_US_TAKER_FEE_BPS / 10_000 × P` per contract
  (default 10 bps = 0.10%). Override via env if their schedule changes.
- The executor uses IOC (Immediate-Or-Cancel) on both legs, so we always
  pay taker fees. Maker rebates, if any, are not relied on.

## Status

**DRY-RUN by default.** Order endpoints are wired end-to-end but neither
Kalshi nor Polymarket US actually receives orders until `DRY_RUN=false`.
Even live, the executor sizes against `MAX_ORDER_USD` and uses IOC TIF
so unmatched orders don't rest on the book.

## Stack

- Python 3.12 + FastAPI + uvicorn
- `kalshi_client.py` — authenticated RSA-PSS-signed REST (stdlib HTTP)
- `polymarket_us_client.py` — thin wrapper around the official
  [`polymarket-us`](https://pypi.org/project/polymarket-us/) SDK
  (Ed25519-signed REST against `https://api.polymarket.us`)
- `pyyaml` for the curated market registry
- Vanilla HTML/JS + Tailwind CDN, no build step

## File layout

```
main.py                 FastAPI app — scan / positions / recommend / execute / discovery
scanner.py              Arb math, registry loader, Kalshi public market data
arb_executor.py         Two-leg concurrent firing + hedge + unwind-on-failure
kalshi_client.py        Authenticated Kalshi client (RSA-PSS)
polymarket_us_client.py Polymarket US SDK wrapper
discovery.py            Cross-venue market discovery (catalog + prefilter + Claude verify)
scan_cli.py             CLI: print arb opportunities to stdout
markets.yaml            Curated pair registry (edit this to add markets)
discovered_pairs.json   Cache of discovery results (auto-written)
static/index.html       Mobile UI
```

## Endpoints

- `GET /` — mobile UI (HTTP Basic auth if `APP_PASSWORD` set)
- `GET /health` — diagnostic; reports `dry_run`, both venues' configured
  state, fee assumptions
- `GET /api/scan?contracts=100&min_spread=-5` — current spreads on every
  enabled pair (5s in-memory cache)
- `GET /api/positions` — per-pair view of Kalshi + Polymarket holdings.
  Hardened to never 500 — venue failures surface in `errors[]`.
- `GET /api/debug/poly` — dumps raw `account.balances()` and
  `portfolio.positions()` responses (or tracebacks). Diagnostic only.
- `POST /api/arb/recommend` — body `{pair_key, direction, contracts,
  allow_over_cap?}`. Validates cap, returns a 60-second confirmation
  token.
- `POST /api/arb/execute` — body `{token}`. Fires both legs IOC, hedges
  imbalance, unwinds if hedge fails. Returns `status` in
  `{success, partial_success, hedged, unwound, naked, failed, dry_run}`.
- `POST /api/discovery/run?do_llm=true` — kick off background catalog
  pull + matching. Returns immediately.
- `GET /api/discovery/status` — running flag, last run summary, whether
  `ANTHROPIC_API_KEY` is wired.
- `GET /api/discovery/candidates?matches_only=true&min_confidence=70` —
  cached candidate pairs with LLM verdicts.
- `POST /api/discovery/promote` — body `{poly_slug, kalshi_ticker, label?,
  yes_means?}`. Appends a pair to `markets.yaml`.

## Registry: `markets.yaml`

Each pair is one stable identifier (`key`), a human label, the exact
Kalshi market ticker, and the exact Polymarket US market slug. Use
`yes_means: inverted` when one venue's YES corresponds to the other's NO.

```yaml
pairs:
  - key: pres-2028-dem-nom
    label: 2028 Dem Nominee — Newsom
    kalshi_ticker: KXPRESPARTY-28-DEM
    polymarket_us_slug: will-gavin-newsom-be-the-2028-dem-nominee
    yes_means: same
    enabled: true
```

The file is re-read on every scan, so adding pairs takes effect without
restarting the server.

## Environment variables

| Var | Required | Notes |
|---|---|---|
| `APP_PASSWORD` | yes for deploy | HTTP Basic password gate. Any username works. |
| `KALSHI_KEY_ID` | for trading + positions | UUID-style Key ID from Kalshi → API Keys |
| `KALSHI_PRIVATE_KEY` | for trading + positions | Full PEM contents. Newlines can be literal `\n`. |
| `POLYMARKET_US_KEY_ID` | for trading + positions | UUID from polymarket.us/developer (requires completed KYC via the Polymarket US iOS app first) |
| `POLYMARKET_US_SECRET_KEY` | for trading + positions | Base64-encoded 32-byte Ed25519 private key |
| `DRY_RUN` | no | `true` (default) stubs orders; `false` = real two-leg execution |
| `MAX_ORDER_USD` | no | Per-execution combined-notional cap (both legs summed). Default 50. |
| `MARKETS_REGISTRY_PATH` | no | Default `./markets.yaml`. |
| `POLY_US_TAKER_FEE_BPS` | no | Polymarket US taker fee in basis points. Default 10 (0.10%). Update if their schedule changes. |
| `MAX_HEDGE_SLIPPAGE_C` | no | Max cents/contract overpay when market-hedging an unfilled leg. Default 2.0. |
| `ANTHROPIC_API_KEY` | for discovery | Claude API key. Without it, /api/discovery falls back to pre-filter-only (no semantic verification). |
| `DISCOVERY_TOP_K` | no | Candidates per Polymarket market the LLM verifies. Default 5. |
| `DISCOVERY_MODEL` | no | Anthropic model ID. Default `claude-haiku-4-5`. |
| `NTFY_TOPIC` | no | ntfy.sh topic for background push notifications. Treat as a secret — anyone with the topic name can read alerts. |
| `NTFY_SERVER` | no | Override ntfy server. Default `https://ntfy.sh`. |
| `NTFY_ARB_SPREAD_C` | no | Alert when locked spread ≥ this many cents/contract. Default 1.0. |
| `NTFY_COOLDOWN_SEC` | no | Min seconds between alerts for the same opportunity. Default 300. |
| `NTFY_INTERVAL_SEC` | no | Background scan interval. Default 30. |

## Local dev

```bash
pip install -r requirements.txt
export KALSHI_KEY_ID=...
export KALSHI_PRIVATE_KEY="$(cat path/to/kalshi_private_key.pem)"
export POLYMARKET_US_KEY_ID=...
export POLYMARKET_US_SECRET_KEY=...
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000 — no `APP_PASSWORD` set means no auth in dev.

## CLI

```bash
python scan_cli.py                            # one-shot, registered pairs
python scan_cli.py --loop 20                  # refresh every 20s
python scan_cli.py --min-spread -1            # include near-miss rows (≥ −1¢)
python scan_cli.py --contracts 50             # size used for fee calc
python scan_cli.py --registry custom.yaml     # different registry file
```

## Railway / production deploy

1. Push to GitHub.
2. Railway → "Deploy from GitHub repo".
3. Set env vars listed above (leave `DRY_RUN=true` until you've verified
   the registry and credentials).
4. Generate domain. Open on phone, Safari → Share → Add to Home Screen.

## Discovery

`/api/discovery/run` pulls every open market from both venues, runs a
TF-IDF prefilter to narrow each Polymarket market to its top-K Kalshi
candidates, then sends each candidate pair to Claude with both sides'
resolution rules and end dates. Claude returns `{match, inverted_yes,
confidence, reason}` per candidate. Results land in
`discovered_pairs.json` and surface in the **Discover** tab.

Tap **Add to registry** on a candidate to write a new pair into
`markets.yaml` — the scanner picks it up on the next tick (file is
re-read every scan, no restart needed).

The "inverted" flag matters: some venues phrase opposite sides of the
same event (one's YES = the other's NO). Promotion preserves whatever
the LLM tagged.

## Safety

- **Confirmation token flow**: `/api/arb/execute` requires a token issued
  by `/api/arb/recommend` within the last 60 seconds. You can't fire by
  accident from a URL.
- **MAX_ORDER_USD** caps the **combined** notional across both legs.
- **IOC time-in-force** on both legs: an unmatched order does not rest
  on the book.
- **Hedge-on-imbalance**: if Kalshi fills 100 and Polymarket fills 80,
  the executor first tries to buy 20 more Polymarket shares at quoted
  + slippage cap. If THAT also fails, it sells back 80 Kalshi at the
  bid — strictly better than leaving naked directional exposure.
- **Naked-exposure detection**: if hedge AND unwind both fail, the
  result returns `status: "naked"` with a `naked_exposure` block telling
  you exactly what to close manually. UI shows a flashing red banner.
- Every execution attempt is appended to `orders.log.jsonl`.

## What's NOT included

- **Polymarket US has no public sandbox.** Live testing starts with real
  money at minimum sizes ($1–5) once you've verified credentials work.
- **No automatic pair discovery.** You curate `markets.yaml` manually
  using market titles, expirations, and resolution criteria you've
  verified match on both venues.
- **No order book depth modeling.** The scanner uses top-of-book best
  bid/ask. If an arb looks tasty at small size but the book is thin past
  the first level, you'll get a partial fill and the hedge logic kicks
  in. Size accordingly.
