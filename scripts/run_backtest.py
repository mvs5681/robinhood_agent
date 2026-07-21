#!/usr/bin/env python3
"""CLI entry point for running the GEX-anchored options backtest harness.

Usage:
    python scripts/run_backtest.py \\
        --tickers SPY \\
        --start 2025-01-02 \\
        --end 2025-06-30 \\
        --fixtures data/history \\
        --capital 2000 \\
        --csv-out results/2025_h1
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import dataclasses
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from trader.backtest.data_store import DataStore
from trader.backtest.harness import BacktestHarness
from trader.backtest.metrics import BacktestResult
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
    p.add_argument(
        "--capital",
        type=float,
        default=2000.0,
        metavar="DOLLARS",
        help="Starting portfolio capital in USD (default: 2000)",
    )
    p.add_argument(
        "--max-trade-pct",
        type=float,
        default=0.25,
        metavar="PCT",
        help="Max fraction of available cash per trade (default: 0.25 = 25%%)",
    )
    p.add_argument(
        "--csv-out",
        metavar="DIR",
        help="Directory to write CSV results (trades.csv, equity.csv, summary.csv)",
    )
    p.add_argument(
        "--bypass-flow-gate",
        action="store_true",
        default=False,
        help=(
            "Skip the FlowTrigger gate (Phase 4) and treat all GEX-scored candidates as "
            "flow-confirmed. Use when backtesting with Polygon data, which has no flow alerts."
        ),
    )
    p.add_argument("--json", action="store_true", help="Print metrics as JSON")
    p.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    return p.parse_args()


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def _write_csv(result: BacktestResult, start: date, end: date, out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # trades.csv — one row per trade
    with open(out / "trades.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "entry_date", "exit_date", "status",
            "ticker", "option_type", "strike", "expiry",
            "contracts", "entry_premium", "entry_cost",
            "exit_premium", "exit_reason",
            "pnl_pct", "pnl_dollars",
            "regime", "setup_type", "composite_score",
        ])
        for r in result.records:
            pos = r.position
            sig = r.exit_signal
            w.writerow([
                r.entry_date,
                r.exit_date or "",
                r.status,
                pos.ticker,
                pos.contract.type,
                float(pos.contract.strike),
                pos.contract.expiry,
                pos.contracts,
                round(float(pos.entry_premium), 4),
                round(float(pos.entry_cost), 2),
                round(float(sig.current_premium), 4) if sig else "",
                sig.reason.value if sig else "",
                round(r.pnl_pct, 4) if r.pnl_pct is not None else "",
                round(r.pnl_dollars, 2) if r.pnl_dollars is not None else "",
                r.candidate.gex_setup.regime.value,
                r.candidate.gex_setup.setup_type or "",
                round(r.candidate.blend_scores.composite, 4),
            ])

    # equity.csv — daily portfolio value
    p = result.portfolio
    if p and p.equity_curve:
        with open(out / "equity.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "portfolio_value", "return_pct"])
            prev = p.initial_capital
            for d, v in p.equity_curve:
                day_return = (v - prev) / prev if prev else 0.0
                w.writerow([d, round(v, 2), round(day_return, 4)])
                prev = v

    # summary.csv — key metrics in a single flat file
    m = result.overall
    rows = [
        ("start_date", start),
        ("end_date", end),
        ("tickers", " ".join(sorted({r.position.ticker for r in result.records}))),
        ("trade_count", m.trade_count),
        ("closed_count", m.closed_count),
        ("win_count", m.win_count),
        ("win_rate", round(m.win_rate, 4)),
        ("avg_pnl_pct", round(m.avg_pnl_pct, 4)),
        ("total_pnl_pct", round(m.total_pnl_pct, 4)),
        ("max_drawdown_pct", round(m.max_drawdown, 4)),
    ]
    if p:
        rows += [
            ("initial_capital", round(p.initial_capital, 2)),
            ("final_value", round(p.final_value, 2)),
            ("total_pnl_dollars", round(p.total_pnl_dollars, 2)),
            ("total_return_pct", round(p.total_return_pct, 4)),
            ("peak_value", round(p.peak_value, 2)),
            ("max_drawdown_dollars", round(p.max_drawdown_dollars, 2)),
            ("max_drawdown_pct_portfolio", round(p.max_drawdown_pct, 4)),
        ]
    for regime, rm in sorted(result.by_regime.items()):
        rows += [
            (f"regime_{regime}_trades", rm.trade_count),
            (f"regime_{regime}_win_rate", round(rm.win_rate, 4)),
            (f"regime_{regime}_avg_pnl_pct", round(rm.avg_pnl_pct, 4)),
        ]

    with open(out / "summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "value"])
        w.writerows(rows)

    print(f"\n  CSV results written to {out.resolve()}/")
    print(f"    trades.csv   — {len(result.records)} trade records")
    if p and p.equity_curve:
        print(f"    equity.csv   — {len(p.equity_curve)} daily data points")
    print(f"    summary.csv  — key metrics")


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------

def _print_results(result: BacktestResult, start: date, end: date,
                   json_mode: bool) -> None:
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

    p = result.portfolio
    if p:
        sign = "+" if p.total_pnl_dollars >= 0 else ""
        print(f"\n=== Portfolio Simulation  (starting ${p.initial_capital:,.2f}) ===")
        print(f"  Final value:    ${p.final_value:,.2f}  ({p.total_return_pct:+.1%})")
        print(f"  Total P&L:      {sign}${p.total_pnl_dollars:,.2f}")
        print(f"  Peak value:     ${p.peak_value:,.2f}")
        print(f"  Max drawdown:   ${p.max_drawdown_dollars:,.2f}  ({p.max_drawdown_pct:.1%})")

        trade_rows = [r for r in result.records
                      if r.status == "closed" and r.pnl_dollars is not None]
        if trade_rows:
            print(f"\n  {'Date':10s}  {'Ticker':6s}  {'Type':4s}  {'Ctrs':4s}  "
                  f"{'Cost':>8s}  {'P&L $':>10s}  {'P&L %':>7s}  Reason")
            print("  " + "-" * 72)
            for r in trade_rows:
                pos = r.position
                sig = r.exit_signal
                print(
                    f"  {r.entry_date}  {pos.ticker:6s}  {pos.contract.type:4s}  "
                    f"{pos.contracts:4d}  "
                    f"${float(pos.entry_cost):>7,.2f}  "
                    f"${r.pnl_dollars:>+9,.2f}  "
                    f"{r.pnl_pct:>+6.1%}  "
                    f"{sig.reason.value}"
                )

        if p.equity_curve:
            curve = p.equity_curve
            step = max(1, len(curve) // 10)
            sampled = curve[::step]
            if curve[-1] not in sampled:
                sampled.append(curve[-1])
            print(f"\n  --- Equity Curve ---")
            for d, v in sampled:
                bar_len = int((v / p.initial_capital - 0.5) * 40)
                bar = ("█" * max(0, bar_len)).ljust(40)
                print(f"  {d}  ${v:>9,.2f}  {bar}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    store = DataStore(args.fixtures)
    policy = StandardPolicy(
        min_composite_score=args.min_composite,
        bypass_flow_gate=args.bypass_flow_gate,
    )
    harness = BacktestHarness(
        policy=policy,
        data_store=store,
        start_date=start,
        end_date=end,
        tickers=args.tickers,
        max_concurrent_positions=args.max_positions,
        initial_capital=args.capital,
        max_trade_pct=args.max_trade_pct,
    )

    result = asyncio.run(harness.run())
    _print_results(result, start, end, args.json)

    if args.csv_out:
        _write_csv(result, start, end, args.csv_out)


if __name__ == "__main__":
    main()
