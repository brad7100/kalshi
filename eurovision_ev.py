"""CLI front-end for the Eurovision EV scanner. The web app (main.py)
shares the same logic via scanner.py.

Run:
    python eurovision_ev.py                     # one-shot
    python eurovision_ev.py --loop 20           # refresh every 20s
    python eurovision_ev.py --contracts 50      # size the Kalshi fee calc
    python eurovision_ev.py --price-side bid    # conservative true_prob
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
        print("No overlapping Kalshi/Polymarket countries found.")
        return
    hdr = (
        f"{'Country':<14}{'K bid/ask':>13}{'P bid/ask':>13}"
        f"{'TrueP':>8}{'Edge(c)':>9}{'EV/$':>9}"
        f"{'K vol':>11}{'P vol':>13}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        kba = f"{r['kalshi_bid']:.3f}/{r['kalshi_ask']:.3f}"
        pba = f"{(r['poly_bid'] or 0):.3f}/{(r['poly_ask'] or 0):.3f}"
        flag = "  +EV" if r["ev_per_dollar"] > 0 else ""
        print(
            f"{r['country']:<14}{kba:>13}{pba:>13}"
            f"{r['true_prob']:>8.3f}{r['edge_pp']:>+9.2f}"
            f"{r['ev_per_dollar']:>+9.2%}"
            f"{r['kalshi_volume']:>11,.0f}{r['poly_volume']:>13,.0f}{flag}"
        )


def run_once(contracts: int, price_side: str) -> None:
    data = scanner.run_scan(contracts=contracts, price_side=price_side)
    print(
        f"\n=== Eurovision 2026 EV @ {datetime.now():%Y-%m-%d %H:%M:%S} "
        f"| Polymarket {price_side} | {contracts}-contract fee ==="
    )
    for err in data.get("errors", []):
        print(f"  ! {err}", file=sys.stderr)
    pos = [r for r in data["rows"] if r["ev_per_dollar"] > 0]
    if pos:
        print(f"+EV signals ({len(pos)}):")
        for r in pos:
            print(
                f"  {r['country']:<14} Kalshi {r['kalshi_ask']:.3f} "
                f"| Poly {r['true_prob']:.3f} "
                f"| EV/$ {r['ev_per_dollar']:+.2%} -> {r['ticker']}"
            )
    else:
        print("(no +EV opportunities at current prices)")
    print()
    print_table(data)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--loop", type=int, default=0,
                   help="Refresh every N seconds (0 = run once)")
    p.add_argument("--contracts", type=int, default=100)
    p.add_argument("--price-side", choices=["bid", "ask", "mid", "last"],
                   default="mid")
    args = p.parse_args()
    if args.loop <= 0:
        run_once(args.contracts, args.price_side)
        return 0
    try:
        while True:
            run_once(args.contracts, args.price_side)
            time.sleep(args.loop)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
