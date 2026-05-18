"""CLI front-end for the arb scanner.

    python scan_cli.py                          # one-shot, all registered pairs
    python scan_cli.py --loop 20                # refresh every 20s
    python scan_cli.py --contracts 50           # size the Kalshi fee calc
    python scan_cli.py --min-spread -1          # include near-misses (1c neg)
    python scan_cli.py --registry alt.yaml      # use a different registry
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

import scanner


def print_table(data: dict) -> None:
    rows = data.get("rows") or []
    if not rows:
        print("No arb opportunities at or above the threshold.")
        return
    hdr = (
        f"{'Pair':<32}{'Dir':>4}{'Kalshi':>12}{'Poly':>12}"
        f"{'Cost':>8}{'Spread (c)':>13}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        kl = r["kalshi_leg"]
        pl = r["poly_leg"]
        k_cell = f"{kl['side'].upper()} {kl['price']:.3f}"
        p_cell = f"{pl['side'].upper()} {pl['price']:.3f}"
        print(
            f"{r['label'][:31]:<32}{r['direction']:>4}"
            f"{k_cell:>12}{p_cell:>12}"
            f"{r['cost_per_contract']:>8.3f}"
            f"{r['locked_spread_cents']:>+13.2f}"
        )


def run_once(contracts: int, min_spread: float, registry: str | None) -> None:
    data = scanner.run_scan(
        contracts=contracts,
        min_spread_cents=min_spread,
        registry_path=registry,
    )
    print(
        f"\n=== Arb scan @ {datetime.now():%Y-%m-%d %H:%M:%S} "
        f"| contracts={contracts} | min_spread={min_spread:+.2f}c "
        f"| poly fee={data['poly_us_taker_fee_bps']:.1f}bps ==="
    )
    for err in data.get("errors", []):
        print(f"  ! {err}", file=sys.stderr)
    if not data["pairs"]:
        print("No pairs in markets.yaml. Add entries under the `pairs:` key.")
        return
    rows = data["rows"]
    pos = [r for r in rows if r["locked_spread_cents"] > 0]
    print(f"Pairs scanned: {len(data['pairs'])} | "
          f"Rows >= threshold: {len(rows)} | "
          f"Positive arbs: {len(pos)}\n")
    if pos:
        print("ARB SIGNALS:")
        for r in pos:
            print(
                f"  {r['label']:<32} [{r['direction']}] "
                f"+{r['locked_spread_cents']:.2f}c -> "
                f"{r['kalshi_leg']['side'].upper()}@Kalshi "
                f"{r['kalshi_leg']['price']:.3f} + "
                f"{r['poly_leg']['side'].upper()}@PolyUS "
                f"{r['poly_leg']['price']:.3f}"
            )
        print()
    print_table(data)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--loop", type=int, default=0,
                   help="Refresh every N seconds (0 = run once)")
    p.add_argument("--contracts", type=int, default=100,
                   help="Contract count used to size fees (default 100)")
    p.add_argument("--min-spread", type=float, default=0.0,
                   help="Minimum locked spread in cents/contract (default 0)")
    p.add_argument("--registry", type=str, default=None,
                   help="Path to markets.yaml (default ./markets.yaml or "
                        "MARKETS_REGISTRY_PATH env)")
    args = p.parse_args()
    if args.loop <= 0:
        run_once(args.contracts, args.min_spread, args.registry)
        return 0
    try:
        while True:
            run_once(args.contracts, args.min_spread, args.registry)
            time.sleep(args.loop)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
