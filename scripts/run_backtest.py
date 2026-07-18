#!/usr/bin/env python3
"""CLI entry point for running the GEX-anchored options backtest harness.

Usage:
    python scripts/run_backtest.py \\
        --tickers AAPL SPY \\
        --start 2026-01-02 \\
        --end 2026-01-05 \\
        --fixtures tests/fixtures/history \\
        --max-positions 3
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
from datetime import date
from pathlib import Path

# Allow running from repo root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from trader.backtest.data_store import DataStore
from trader.backtest.harness import BacktestHarness
from trader.backtest.policy import StandardPolicy


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="GEX backtest harness")
    p.add_argument("--tickers", nargs="+", required=True, metavar="TICKER")
    p.add_argument("--start", required=True, metavar="YYYY-MM-DD")
    p.add_argument("--end", required=True, metavar="YYYY-MM-DD")
    p.add_argument(
        "--fixtures",
        default="tests/fixtures/history",
        help="Root directory containing YYYY-MM-DD fixture subdirectories",
    )
    p.add_argument("--max-positions", type=int, default=3, metavar="N")
    p.add_argument(
        "--min-composite",
        type=float,
        default=0.0,
        metavar="SCORE",
        help="Minimum blend composite score required to enter (0–1)",
    )
    p.add_argument("--json", action="store_true", help="Print metrics as JSON")
    p.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    return p.parse_args()


def _print_results(result, start: date, end: date, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(dataclasses.asdict(result.overall), indent=2))
        return

    m = result.overall
    print(f"\n=== Backtest Results  {start} → {end} ===")
    print(f"  Trades:     {m.trade_count} total  ({m.closed_count} closed)")
    print(f"  Win rate:   {m.win_rate:.1%}")
    print(f"  Avg P&L:    {m.avg_pnl_pct:+.1%}")
    print(f"  Max DD:     {m.max_drawdown:+.1%}")
    print(f"  Total P&L:  {m.total_pnl_pct:+.1%}")

    if result.by_regime:
        print("\n  --- By Regime ---")
        for regime, metrics in sorted(result.by_regime.items()):
            print(
                f"  {regime:12s}  {metrics.trade_count:3d} trades  "
                f"WR {metrics.win_rate:.0%}  avg {metrics.avg_pnl_pct:+.1%}"
            )

    if result.by_setup_type:
        print("\n  --- By Setup Type ---")
        for setup, metrics in sorted(result.by_setup_type.items()):
            print(
                f"  {setup:12s}  {metrics.trade_count:3d} trades  "
                f"WR {metrics.win_rate:.0%}  avg {metrics.avg_pnl_pct:+.1%}"
            )

    # Exit reason breakdown
    closed = [r for r in result.records if r.status == "closed" and r.exit_signal]
    if closed:
        from collections import Counter
        reason_counts = Counter(r.exit_signal.reason.value for r in closed)
        print("\n  --- Exit Reasons ---")
        for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
            pnls = [r.pnl_pct for r in closed
                    if r.exit_signal.reason.value == reason and r.pnl_pct is not None]
            avg = sum(pnls) / len(pnls) if pnls else 0.0
            print(f"  {reason:16s}  {count:3d}x  avg {avg:+.1%}")


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    store = DataStore(args.fixtures)
    policy = StandardPolicy(min_composite_score=args.min_composite)
    harness = BacktestHarness(
        policy=policy,
        data_store=store,
        start_date=start,
        end_date=end,
        tickers=args.tickers,
        max_concurrent_positions=args.max_positions,
    )

    result = asyncio.run(harness.run())
    _print_results(result, start, end, args.json)


if __name__ == "__main__":
    main()
