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
import json
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


def _count(raw) -> int:
    if isinstance(raw, list):
        return len(raw)
    if isinstance(raw, dict):
        data = raw.get("data", raw)
        return len(data) if isinstance(data, list) else 1
    return 0


def _status(n: int) -> str:
    return "OK" if n > 0 else "EMPTY"


async def _run(ticker: str, test_date: date) -> None:
    print(f"\nConnecting to UW MCP…")
    tools_list = await load_uw_tools()
    tbn = tools_by_name(tools_list)
    print(f"Connected. {len(tbn)} tools available: {sorted(tbn)}\n")

    date_str = test_date.isoformat()
    next_day = (test_date + timedelta(days=1)).isoformat()

    results: list[tuple[str, int, str]] = []

    async def _check(label: str, tool_name: str, kwargs: dict) -> None:
        if tool_name not in tbn:
            results.append((label, -1, "MISSING TOOL"))
            return
        try:
            raw = await tbn[tool_name].ainvoke(kwargs)
            n = _count(raw)
            results.append((label, n, _status(n)))
            if n > 0:
                items = raw.get("data", raw) if isinstance(raw, dict) else raw
                sample = items[0] if isinstance(items, list) and items else items
                # Print first record keys so user can validate the shape
                if isinstance(sample, dict):
                    print(f"  {label}: sample keys = {list(sample.keys())[:8]}")
        except Exception as exc:
            results.append((label, 0, f"ERROR: {exc}"))

    # Test each endpoint that fetch_history.py uses
    await _check(
        "get_market_tide (historical)",
        "get_market_tide",
        {"date": date_str},
    )
    await _check(
        "get_flow_alerts (historical)",
        "get_flow_alerts",
        {
            "limit": 50,
            "newer_than": date_str,
            "older_than": next_day,
        },
    )
    await _check(
        "get_greek_exposure_by_strike",
        "get_greek_exposure_by_strike",
        {"ticker": ticker, "date": date_str},
    )
    await _check(
        "get_dark_pool_trades",
        "get_dark_pool_trades",
        {"ticker_symbol": ticker, "date": date_str, "limit": 20},
    )
    await _check(
        "get_flow_per_strike",
        "get_flow_per_strike",
        {"ticker": ticker, "date": date_str},
    )
    await _check(
        "get_options_chain (historical)",
        "get_options_chain",
        {"ticker": ticker, "date": date_str, "limit": 20},
    )
    await _check(
        "get_extended_technical_indicator (RSI, no date)",
        "get_extended_technical_indicator",
        {"ticker": ticker, "function": "RSI", "interval": "daily"},
    )

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
        print(f"  → UW may not have data for {ticker} on {test_date}.")
        print("  → Try a more recent date (last 6 months is usually available).")
        print("  → Index tickers (SPY) tend to have better coverage than single stocks.")

    if not errors and not empty:
        print(f"All endpoints returned data for {ticker} on {test_date}.")
        print("You can now run fetch_history.py to download a full date range.")


def main() -> None:
    p = argparse.ArgumentParser(description="Validate UW historical API access")
    p.add_argument("--ticker", default="SPY", help="Ticker to test (default: SPY)")
    p.add_argument(
        "--date",
        default=None,
        help="Historical date YYYY-MM-DD (default: 30 days ago)",
    )
    args = p.parse_args()

    if args.date:
        test_date = date.fromisoformat(args.date)
    else:
        # Default to 30 days ago — recent enough to have data, clearly historical
        test_date = date.today() - timedelta(days=30)
        # Shift to Friday if it lands on a weekend
        while test_date.weekday() >= 5:
            test_date -= timedelta(days=1)

    print(f"Testing UW historical API — ticker={args.ticker}  date={test_date}")
    asyncio.run(_run(args.ticker, test_date))


if __name__ == "__main__":
    main()
