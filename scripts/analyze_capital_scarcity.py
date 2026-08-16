#!/usr/bin/env python3
"""Validation gate for the capital-reallocation plan (docs/CAPITAL_REALLOCATION.md).

Measures how often the agent is capital/slot-constrained (all
`--max-positions` slots full) while a materially better new candidate
exists, and how large the score gap is when it does. This is Phase 0's
gate: re-run this after fixing `StandardPolicy.should_exit()`'s missing
thesis-invalidation (see TODO.md) and only proceed to Phase 2 design work
if real, non-noise score gaps remain.

Usage:
    python scripts/analyze_capital_scarcity.py --history data/history
    python scripts/analyze_capital_scarcity.py --history data/history \\
        --max-positions 3 --capital 2000 --gap-threshold 0.15
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from trader.backtest.data_store import DataStore
from trader.backtest.harness import BacktestHarness
from trader.backtest.policy import StandardPolicy
from trader.live.state_capture import StateCapture


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--history",
        default="data/history",
        help="Root directory containing YYYY-MM-DD capture subdirectories (default: data/history)",
    )
    p.add_argument("--max-positions", type=int, default=3, metavar="N")
    p.add_argument(
        "--capital",
        type=float,
        default=2000.0,
        metavar="DOLLARS",
        help="Starting portfolio capital in USD (default: 2000)",
    )
    p.add_argument(
        "--min-coverage-days",
        type=int,
        default=3,
        metavar="N",
        help="Minimum captured days a ticker needs to be included in the universe (default: 3)",
    )
    p.add_argument(
        "--gap-threshold",
        type=float,
        default=0.15,
        metavar="SCORE",
        help="Composite-score gap above which a scarcity event counts as a real edge, not noise (default: 0.15)",
    )
    return p.parse_args()


async def _run(args: argparse.Namespace) -> None:
    store = DataStore(args.history)
    available = store.available_dates()
    if not available:
        print(f"No captured history found under {args.history}")
        return
    start, end = available[0], available[-1]

    capture = StateCapture(args.history)
    tickers = sorted(capture.covered_tickers(min_days=args.min_coverage_days).keys())
    print(f"Universe: {len(tickers)} tickers, {len(available)} days ({start} -> {end})")

    policy = StandardPolicy(bypass_flow_gate=True)
    harness = BacktestHarness(
        policy=policy,
        data_store=store,
        start_date=start,
        end_date=end,
        tickers=tickers,
        max_concurrent_positions=args.max_positions,
        initial_capital=args.capital,
    )

    daily_snapshots = []
    state = None
    for trade_date in available:
        if not (start <= trade_date <= end):
            continue
        open_before = list(state.open_positions) if state else []
        data_slice = store.load(trade_date)
        candidates = await policy.generate_and_score(tickers, data_slice)
        daily_snapshots.append((trade_date, open_before, candidates))
        state = await harness.step_forward(state)

    scarcity_events = []
    for trade_date, open_before, candidates in daily_snapshots:
        if len(open_before) < args.max_positions:
            continue
        held_tickers = {p.ticker for p in open_before}
        by_ticker = {c.ticker: c for c in candidates}

        held_scores = [
            (pos.ticker, by_ticker[pos.ticker].blend_scores.composite)
            for pos in open_before
            if pos.ticker in by_ticker
        ]
        new_proposed = [
            c for c in candidates
            if c.ticker not in held_tickers and c.execution_status == "proposed"
        ]
        if new_proposed and held_scores:
            weakest_ticker, weakest_score = min(held_scores, key=lambda x: x[1])
            for c in new_proposed:
                gap = c.blend_scores.composite - weakest_score
                scarcity_events.append({
                    "date": trade_date,
                    "new_ticker": c.ticker,
                    "new_score": c.blend_scores.composite,
                    "weakest_held": weakest_ticker,
                    "weakest_score": weakest_score,
                    "gap": gap,
                })

    days_full = sum(1 for _, o, _ in daily_snapshots if len(o) >= args.max_positions)
    print(f"\nDays at {args.max_positions}/{args.max_positions} capacity: {days_full} / {len(daily_snapshots)}")
    print(f"Scarcity events (new proposable candidate while full): {len(scarcity_events)}")

    if scarcity_events:
        gaps = sorted(e["gap"] for e in scarcity_events)
        print(
            f"Composite-score gap (new vs weakest held): "
            f"min={gaps[0]:.3f} median={gaps[len(gaps) // 2]:.3f} max={gaps[-1]:.3f}"
        )
        over = sum(1 for g in gaps if g > args.gap_threshold)
        print(f"Events with gap > {args.gap_threshold:.2f} (a real edge, not noise): {over}")
        print("\nAll events:")
        for e in scarcity_events:
            print(
                f"  {e['date']}  new={e['new_ticker']}({e['new_score']:.3f})  "
                f"vs weakest_held={e['weakest_held']}({e['weakest_score']:.3f})  gap={e['gap']:+.3f}"
            )

    print(f"\nFinal simulated position count: {len(state.open_positions) if state else 0}")
    print(f"Total trades entered over the window: {len(state.records) if state else 0}")


def main() -> None:
    args = _parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
