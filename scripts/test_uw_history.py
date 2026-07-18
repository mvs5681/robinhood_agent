#!/usr/bin/env python3
"""
Sanity check: verify UW API can return historical data before running fetch_history.py.

Tests all six endpoints used by the backtest fetcher against a single date and ticker.
Set UW_API_TOKEN in your .env (or export it) before running.

Usage:
    python scripts/test_uw_history.py
    python scripts/test_uw_history.py --ticker AAPL --date 2025-03-01
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
    _unwrap,
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
    raw = None
    try:
        raw = await tbn[tool_name].ainvoke(kwargs)
        records = parser(raw)
        n = len(records)
        status = "OK" if n > 0 else "EMPTY"
        if n > 0:
            r = records[0]
            sample = {k: v for k, v in vars(r).items() if v is not None}
            keys = list(sample.keys())[:5]
            print(f"  {label}: {keys}")
        return label, n, status
    except Exception as exc:
        # Print raw shape so the format issue is diagnosable without a debugger
        if raw is not None:
            if isinstance(raw, list) and raw and isinstance(raw[0], dict):
                text_val = raw[0].get("text", "(no text key)")
                text_preview = repr(text_val)[:200] if not isinstance(text_val, str) else text_val[:200]
                print(f"  RAW block[0] keys={list(raw[0].keys())}  text type={type(text_val).__name__}  preview={text_preview}")
            else:
                print(f"  RAW type={type(raw).__name__}  preview={repr(raw)[:200]}")
        return label, 0, f"ERROR: {exc}"


async def _run(ticker: str, test_date: date) -> None:
    print(f"\nConnecting to UW MCP…")
    tools_list = await load_uw_tools()
    tbn = tools_by_name(tools_list)
    print(f"Connected. {len(tbn)} tools available.\n")

    date_str = test_date.isoformat()
    next_day = (test_date + timedelta(days=1)).isoformat()

    checks = [
        ("get_market_tide (historical)",
         "get_market_tide", {"date": date_str},
         parse_market_tide),

        ("get_flow_alerts (historical)",
         "get_flow_alerts",
         {"limit": 50, "newer_than": date_str, "older_than": next_day},
         parse_flow_alerts),

        ("get_greek_exposure_by_strike",
         "get_greek_exposure_by_strike",
         {"ticker": ticker, "date": date_str},
         parse_spot_gex_by_strike),

        ("get_dark_pool_trades",
         "get_dark_pool_trades",
         {"ticker_symbol": ticker, "date": date_str, "limit": 20},
         parse_darkpool_prints),

        ("get_flow_per_strike",
         "get_flow_per_strike",
         {"ticker": ticker, "date": date_str},
         parse_net_prem_ticks),

        ("get_options_chain (historical)",
         "get_options_chain",
         {"ticker": ticker, "date": date_str, "limit": 20},
         parse_option_contracts),

        ("get_extended_technical_indicator",
         "get_extended_technical_indicator",
         {"ticker": ticker, "function": "RSI", "interval": "daily"},
         lambda raw: parse_technical_indicator(raw, "RSI")),
    ]

    results = []
    for label, tool_name, kwargs, parser in checks:
        results.append(await _check(tbn, label, tool_name, kwargs, parser))

    print(f"\n{'Endpoint':<44} {'Records':>8}  Status")
    print("-" * 62)
    for label, n, status in results:
        n_str = str(n) if n >= 0 else "—"
        ok = "✓" if status == "OK" else ("?" if status == "EMPTY" else "✗")
        print(f"  {ok}  {label:<42} {n_str:>6}   {status}")

    empty = [label for label, n, s in results if s == "EMPTY"]
    errors = [label for label, n, s in results if "ERROR" in s or "MISSING" in s]

    print()
    if errors:
        print(f"ERRORS ({len(errors)}): {errors}")
        print("  → Check UW_API_TOKEN is set and the MCP server is reachable.")
    if empty:
        print(f"EMPTY ({len(empty)}): {empty}")
        print(f"  → UW has no data for {ticker} on {test_date}.")
        print("  → Try a more recent date or a higher-volume ticker like SPY.")
    if not errors and not empty:
        print(f"All endpoints returned real records for {ticker} on {test_date}.")
        print("You can now run fetch_history.py to download a full date range.")


def main() -> None:
    p = argparse.ArgumentParser(description="Validate UW historical API access")
    p.add_argument("--ticker", default="SPY", help="Ticker to test (default: SPY)")
    p.add_argument("--date", default=None, help="Date YYYY-MM-DD (default: 30 days ago)")
    args = p.parse_args()

    if args.date:
        test_date = date.fromisoformat(args.date)
    else:
        test_date = date.today() - timedelta(days=30)
        while test_date.weekday() >= 5:
            test_date -= timedelta(days=1)

    print(f"Testing UW historical API — ticker={args.ticker}  date={test_date}")
    asyncio.run(_run(args.ticker, test_date))


if __name__ == "__main__":
    main()
