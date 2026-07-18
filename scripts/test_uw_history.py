#!/usr/bin/env python3
"""
Validate UW API access before running fetch_history.py or capture_today.py.

Token: set UW_API_TOKEN in your .env (or export it) before running.

Usage:
    python scripts/test_uw_history.py
    python scripts/test_uw_history.py --ticker AAPL --date 2026-06-01
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from trader.uw.mcp_config import load_uw_tools, tools_by_name
from trader.uw.validators import (
    parse_darkpool_prints,
    parse_flow_alerts,
    parse_market_tide,
    parse_net_prem_ticks,
    parse_option_contracts,
    parse_spot_gex_by_strike,
    parse_technical_indicator,
)


async def _check(tbn: dict, label: str, tool_name: str, kwargs: dict, parser) -> tuple[str, int, str]:
    if tool_name not in tbn:
        return label, -1, "MISSING TOOL"
    try:
        raw = await tbn[tool_name].ainvoke(kwargs)
        records = parser(raw)
        n = len(records)
        status = "OK" if n > 0 else "EMPTY"
        if n > 0:
            r = records[0]
            keys = [k for k, v in vars(r).items() if v is not None][:5]
            print(f"  {label}: {keys}")
        return label, n, status
    except Exception as exc:
        if hasattr(exc, '__context__') and exc.__context__:
            print(f"  ERROR detail: {exc.__context__}")
        return label, 0, f"ERROR: {str(exc)[:120]}"


async def _run(ticker: str, test_date: date) -> None:
    print(f"\nConnecting to UW MCP…")
    tools_list = await load_uw_tools()
    tbn = tools_by_name(tools_list)
    print(f"Connected. {len(tbn)} tools available.\n")

    date_str = test_date.isoformat()

    print("── Historical endpoints (date= supported) ──────────────────────")
    hist_checks = [
        ("get_market_tide",
         "get_market_tide", {"date": date_str},
         parse_market_tide),
        ("get_greek_exposure_by_strike",
         "get_greek_exposure_by_strike", {"ticker": ticker, "date": date_str},
         parse_spot_gex_by_strike),
        ("get_flow_per_strike",
         "get_flow_per_strike", {"ticker": ticker, "date": date_str},
         parse_net_prem_ticks),
    ]

    print("\n── Current-only endpoints (no date= support — daily capture) ───")
    current_checks = [
        ("get_flow_alerts (current)",
         "get_flow_alerts",
         {"limit": 50, "min_premium": "250000", "issue_types": ["Index", "ETF"]},
         parse_flow_alerts),
        ("get_dark_pool_trades (current)",
         "get_dark_pool_trades",
         {"ticker_symbol": ticker, "limit": 20},
         parse_darkpool_prints),
        ("get_options_chain (current)",
         "get_options_chain",
         {"ticker": ticker, "limit": 20},
         parse_option_contracts),
        ("get_extended_technical_indicator",
         "get_extended_technical_indicator",
         {"ticker": ticker, "function": "RSI", "interval": "daily"},
         lambda raw: parse_technical_indicator(raw, "RSI")),
    ]

    hist_results = []
    for label, tool_name, kwargs, parser in hist_checks:
        hist_results.append(await _check(tbn, label, tool_name, kwargs, parser))

    current_results = []
    for label, tool_name, kwargs, parser in current_checks:
        current_results.append(await _check(tbn, label, tool_name, kwargs, parser))

    all_results = hist_results + current_results

    print(f"\n{'Endpoint':<40} {'Records':>8}  Status")
    print("-" * 56)
    print(f"  Historical (date={date_str}):")
    for label, n, status in hist_results:
        n_str = str(n) if n >= 0 else "—"
        ok = "✓" if status == "OK" else ("?" if status == "EMPTY" else "✗")
        print(f"    {ok}  {label:<36} {n_str:>6}   {status}")
    print(f"  Current snapshot (no date param):")
    for label, n, status in current_results:
        n_str = str(n) if n >= 0 else "—"
        ok = "✓" if status == "OK" else ("?" if status == "EMPTY" else "✗")
        print(f"    {ok}  {label:<36} {n_str:>6}   {status}")

    errors = [label for label, n, s in all_results if "ERROR" in s or "MISSING" in s]
    empty = [label for label, n, s in all_results if s == "EMPTY"]

    print()
    if errors:
        print(f"ERRORS ({len(errors)}): {errors}")
        print("  → Check UW_API_TOKEN and MCP connectivity.")
    elif empty:
        print(f"EMPTY ({len(empty)}): {empty}")
        print(f"  → No data for {ticker} on {test_date}. Try a different date or ticker.")
    else:
        print(f"All endpoints OK.")
        print(f"  Historical (GEX/tide/flow-per-strike): ready for date-range fetching.")
        print(f"  Current snapshot: run capture_today.py at market close to accumulate history.")


def main() -> None:
    p = argparse.ArgumentParser(description="Validate UW API access")
    p.add_argument("--ticker", default="SPY")
    p.add_argument("--date", default=None, help="Historical date YYYY-MM-DD (default: 30 days ago)")
    args = p.parse_args()

    if args.date:
        test_date = date.fromisoformat(args.date)
    else:
        test_date = date.today() - timedelta(days=30)
        while test_date.weekday() >= 5:
            test_date -= timedelta(days=1)

    print(f"Testing UW API — ticker={args.ticker}  date={test_date}")
    asyncio.run(_run(args.ticker, test_date))


if __name__ == "__main__":
    main()
