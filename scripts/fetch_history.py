#!/usr/bin/env python3
"""
Fetch historical UW data for a date range and write backtest fixtures.

Each trading day becomes a subdirectory under --out:

    <out>/
      YYYY-MM-DD/
        market_tide.json
        flow_alerts.json
        {TICKER}_spot_gex.json
        {TICKER}_darkpool.json
        {TICKER}_net_prem_ticks.json
        {TICKER}_option_contracts.json
        {TICKER}_technicals_RSI.json
        {TICKER}_technicals_MACD.json

Token: set UW_API_TOKEN in your .env or export it before running.

Usage:
    python scripts/fetch_history.py \\
        --start 2025-06-01 \\
        --end 2025-06-30 \\
        --out data/history \\
        --tickers SPY QQQ

Then run the backtest:
    python scripts/run_backtest.py \\
        --fixtures data/history \\
        --start 2025-06-01 \\
        --end 2025-06-30 \\
        --tickers SPY QQQ
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from trader.uw.mcp_config import load_uw_tools, tools_by_name
from trader.uw.validators import parse_flow_alerts

logger = logging.getLogger(__name__)

_SEM_LIMIT = 3
_DEFAULT_MIN_PREMIUM = 250_000
_DEFAULT_MAX_TICKERS = 20


def _trading_days(start: date, end: date) -> list[date]:
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


async def _call(tool, kwargs: dict) -> dict:
    try:
        return await tool.ainvoke(kwargs)
    except Exception as exc:
        logger.warning("%-30s %s → error: %s", tool.name, kwargs, exc)
        return {"data": []}


async def _fetch_day(
    tools: dict,
    trade_date: date,
    out_dir: Path,
    seeds: list[str],
    min_premium: int,
    max_tickers: int,
    sem: asyncio.Semaphore,
    delay_between_tickers: float,
) -> None:
    day_dir = out_dir / trade_date.isoformat()

    # Skip already-fetched dates (resumable)
    if (day_dir / "market_tide.json").exists():
        logger.info("%s already fetched — skipping", trade_date)
        return

    day_dir.mkdir(parents=True, exist_ok=True)
    logger.info("=== %s ===", trade_date)

    next_day = (trade_date + timedelta(days=1)).isoformat()
    date_str = trade_date.isoformat()

    # ── Market-wide ──────────────────────────────────────────────────────
    async with sem:
        market_tide_raw = await _call(tools["get_market_tide"], {"date": date_str})

    # Two slices so indexes/ETFs aren't crowded out by stocks
    async with sem:
        flow_index = await _call(
            tools["get_flow_alerts"],
            {
                "limit": 200,
                "min_premium": str(min_premium),
                "issue_types": ["Index", "ETF"],
                "newer_than": date_str,
                "older_than": next_day,
            },
        )
    async with sem:
        flow_stocks = await _call(
            tools["get_flow_alerts"],
            {
                "limit": 200,
                "min_premium": str(min_premium),
                "issue_types": ["Common Stock", "ADR"],
                "newer_than": date_str,
                "older_than": next_day,
            },
        )

    # Deduplicate and merge alerts
    all_alerts = []
    seen: set[str] = set()
    for raw in (flow_index, flow_stocks):
        for a in parse_flow_alerts(raw):
            key = f"{a.ticker}:{a.expiry}:{a.strike}:{a.type}:{a.created_at}"
            if key not in seen:
                seen.add(key)
                all_alerts.append(a)

    # Rank by total premium to discover tickers for this day
    premium: dict[str, Decimal] = defaultdict(Decimal)
    for a in all_alerts:
        premium[a.ticker] += a.total_premium

    ranked = sorted(premium, key=lambda t: premium[t], reverse=True)
    discovered = [t for t in ranked if premium[t] >= min_premium][:max_tickers]

    tickers_seen: set[str] = set()
    tickers: list[str] = []
    for t in seeds + discovered:
        if t not in tickers_seen:
            tickers_seen.add(t)
            tickers.append(t)

    logger.info("%s: %d tickers — %s", trade_date, len(tickers), tickers)

    # Save market-wide files
    (day_dir / "market_tide.json").write_text(json.dumps(market_tide_raw))

    combined_alerts: list = []
    for raw in (flow_index, flow_stocks):
        items = raw.get("data", []) if isinstance(raw, dict) else raw
        if isinstance(items, list):
            combined_alerts.extend(items)
    (day_dir / "flow_alerts.json").write_text(json.dumps({"data": combined_alerts}))

    if not tickers:
        logger.warning("%s: no tickers discovered and no seeds provided", trade_date)
        return

    # ── Per-ticker fetches ────────────────────────────────────────────────
    async def _fetch_ticker(ticker: str) -> None:
        async def _save(filename: str, kwargs: dict, tool_name: str) -> None:
            async with sem:
                raw = await _call(tools[tool_name], kwargs)
            (day_dir / filename).write_text(json.dumps(raw))

        await asyncio.gather(
            _save(f"{ticker}_spot_gex.json",
                  {"ticker": ticker, "date": date_str},
                  "get_greek_exposure_by_strike"),
            _save(f"{ticker}_darkpool.json",
                  {"ticker_symbol": ticker, "date": date_str, "limit": 100},
                  "get_dark_pool_trades"),
            _save(f"{ticker}_net_prem_ticks.json",
                  {"ticker": ticker, "date": date_str},
                  "get_flow_per_strike"),
            # options chain with historical date — needed for entry premium + exit replay
            _save(f"{ticker}_option_contracts.json",
                  {"ticker": ticker, "date": date_str, "limit": 100},
                  "get_options_chain"),
            # technicals don't expose a date param — fetch latest (close enough for signals)
            _save(f"{ticker}_technicals_RSI.json",
                  {"ticker": ticker, "function": "RSI", "interval": "daily"},
                  "get_extended_technical_indicator"),
            _save(f"{ticker}_technicals_MACD.json",
                  {"ticker": ticker, "function": "MACD", "interval": "daily"},
                  "get_extended_technical_indicator"),
        )
        logger.info("%s: saved %s", trade_date, ticker)

    for i, ticker in enumerate(tickers):
        await _fetch_ticker(ticker)
        if i < len(tickers) - 1 and delay_between_tickers > 0:
            await asyncio.sleep(delay_between_tickers)

    logger.info("%s: complete — %d tickers → %s", trade_date, len(tickers), day_dir)


async def _main(args: argparse.Namespace) -> None:
    tools_list = await load_uw_tools()
    tbn = tools_by_name(tools_list)

    required = {
        "get_market_tide",
        "get_flow_alerts",
        "get_greek_exposure_by_strike",
        "get_dark_pool_trades",
        "get_flow_per_strike",
        "get_options_chain",
        "get_extended_technical_indicator",
    }
    missing = required - set(tbn.keys())
    if missing:
        logger.error("UW MCP did not expose required tools: %s", missing)
        sys.exit(1)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    seeds = args.tickers or []
    sem = asyncio.Semaphore(_SEM_LIMIT)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    days = _trading_days(start, end)

    logger.info(
        "Fetching %d trading days (%s → %s) → %s",
        len(days), start, end, out_dir,
    )

    for i, d in enumerate(days):
        await _fetch_day(
            tbn, d, out_dir, seeds,
            args.min_premium, args.max_tickers,
            sem, args.ticker_delay,
        )
        if i < len(days) - 1 and args.date_delay > 0:
            await asyncio.sleep(args.date_delay)

    logger.info("Done.")
    logger.info(
        "Run backtest: python scripts/run_backtest.py --fixtures %s --start %s --end %s --tickers %s",
        out_dir, start, end, " ".join(seeds) if seeds else "SPY",
    )


def main() -> None:
    p = argparse.ArgumentParser(
        description="Download historical UW data for backtest replay",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--start", required=True, metavar="YYYY-MM-DD",
                   help="First date to fetch (inclusive)")
    p.add_argument("--end", required=True, metavar="YYYY-MM-DD",
                   help="Last date to fetch (inclusive)")
    p.add_argument("--out", default="data/history", metavar="DIR",
                   help="Output directory (default: data/history)")
    p.add_argument("--tickers", nargs="*", metavar="TICKER",
                   help="Seed tickers always fetched regardless of flow activity")
    p.add_argument("--min-premium", type=int, default=_DEFAULT_MIN_PREMIUM,
                   metavar="USD",
                   help="Min total premium to auto-discover a ticker (default: 250000)")
    p.add_argument("--max-tickers", type=int, default=_DEFAULT_MAX_TICKERS,
                   metavar="N",
                   help="Max auto-discovered tickers per day (default: 20)")
    p.add_argument("--date-delay", type=float, default=2.0, metavar="SEC",
                   help="Seconds to sleep between dates (rate limiting, default: 2)")
    p.add_argument("--ticker-delay", type=float, default=0.5, metavar="SEC",
                   help="Seconds to sleep between tickers within a day (default: 0.5)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Enable debug logging")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
