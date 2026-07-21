#!/usr/bin/env python3
"""
Fetch UW data and write daily backtest fixtures.

UW historical API coverage (as of 2026-07):
  SUPPORTED with date= : get_greek_exposure_by_strike, get_market_tide, get_flow_per_strike
  CURRENT DATA ONLY    : get_flow_alerts, get_dark_pool_trades, get_options_chain

Because flow alerts, dark pool, and options chain don't support historical date
filtering, this script is most useful as a DAILY CAPTURE JOB — run once per
trading day (e.g. at 4:30 PM ET) and accumulate real snapshots the backtest
can replay later.

Run it daily with cron or the companion capture_today.py wrapper.

Usage:
    # Fetch one specific date (GEX/tide/flow-per-strike historical; rest is current snapshot):
    python scripts/fetch_history.py \\
        --start 2026-06-01 --end 2026-06-30 \\
        --out data/history --tickers SPY QQQ

    # Daily capture (run at market close — saves today's full live snapshot):
    python scripts/capture_today.py

Then backtest:
    python scripts/run_backtest.py \\
        --fixtures data/history \\
        --start 2026-06-01 --end 2026-06-30 \\
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
        logger.warning("%-35s → error: %s", tool.name, exc)
        return {"data": []}


async def _discover_tickers(
    tools: dict,
    min_premium: int,
    sem: asyncio.Semaphore,
) -> tuple[list[str], dict[str, Decimal]]:
    """
    Discover high-premium tickers from current flow alerts.
    Note: get_flow_alerts does not support historical date filtering.
    """
    slices = [
        {"limit": 200, "min_premium": str(min_premium), "issue_types": ["Index", "ETF"]},
        {"limit": 200, "min_premium": str(min_premium), "issue_types": ["Common Stock", "ADR"]},
    ]
    results = []
    for params in slices:
        async with sem:
            results.append(await _call(tools["get_flow_alerts"], params))

    all_alerts = []
    seen: set[str] = set()
    spot_hints: dict[str, Decimal] = {}
    for raw in results:
        for a in parse_flow_alerts(raw):
            key = f"{a.ticker}:{a.expiry}:{a.strike}:{a.type}:{a.created_at}"
            if key not in seen:
                seen.add(key)
                all_alerts.append(a)
            if a.underlying_price and a.ticker not in spot_hints:
                spot_hints[a.ticker] = a.underlying_price

    premium: dict[str, Decimal] = defaultdict(Decimal)
    for a in all_alerts:
        premium[a.ticker] += a.total_premium

    ranked = sorted(premium, key=lambda t: premium[t], reverse=True)
    discovered = [t for t in ranked if premium[t] >= min_premium]
    return discovered, spot_hints


async def _save_flow_alerts(tools: dict, sem: asyncio.Semaphore, day_dir: Path, min_premium: int) -> None:
    """Save current flow alerts (no historical filtering available)."""
    combined: list = []
    for params in [
        {"limit": 200, "min_premium": str(min_premium), "issue_types": ["Index", "ETF"]},
        {"limit": 200, "min_premium": str(min_premium), "issue_types": ["Common Stock", "ADR"]},
    ]:
        async with sem:
            raw = await _call(tools["get_flow_alerts"], params)
        items = raw.get("data", []) if isinstance(raw, dict) else raw
        if isinstance(items, list):
            combined.extend(items)
    (day_dir / "flow_alerts.json").write_text(json.dumps({"data": combined}))


async def _fetch_day(
    tools: dict,
    trade_date: date,
    out_dir: Path,
    seeds: list[str],
    min_premium: int,
    max_tickers: int,
    sem: asyncio.Semaphore,
    ticker_delay: float,
    historical_only: bool,
) -> None:
    day_dir = out_dir / trade_date.isoformat()
    if (day_dir / "market_tide.json").exists():
        logger.info("%s already fetched — skipping", trade_date)
        return

    day_dir.mkdir(parents=True, exist_ok=True)
    logger.info("=== %s ===", trade_date)
    date_str = trade_date.isoformat()

    # Market tide — supports historical date=
    async with sem:
        market_tide_raw = await _call(tools["get_market_tide"], {"date": date_str})
    (day_dir / "market_tide.json").write_text(json.dumps(market_tide_raw))

    # Flow alerts — current snapshot only (no date filtering)
    if not historical_only:
        await _save_flow_alerts(tools, sem, day_dir, min_premium)
        discovered, spot_hints = await _discover_tickers(tools, min_premium, sem)
    else:
        # Historical-only mode: skip flow alerts (they'd return today's alerts, misleading)
        (day_dir / "flow_alerts.json").write_text(json.dumps({"data": []}))
        discovered, spot_hints = [], {}
        logger.info("%s: historical-only mode — flow alerts skipped", trade_date)

    # Merge seeds + discovered
    seen_t: set[str] = set()
    tickers: list[str] = []
    for t in seeds + discovered:
        if t not in seen_t:
            seen_t.add(t)
            tickers.append(t)
    tickers = tickers[:max_tickers]

    if not tickers:
        logger.warning("%s: no tickers — pass --tickers to seed at least one", trade_date)
        return

    logger.info("%s: fetching %d tickers: %s", trade_date, len(tickers), tickers)

    async def _fetch_ticker(ticker: str) -> None:
        async def _save(filename: str, tool_name: str, kwargs: dict) -> None:
            async with sem:
                raw = await _call(tools[tool_name], kwargs)
            (day_dir / filename).write_text(json.dumps(raw))

        # Endpoints that support date= for historical data
        await _save(f"{ticker}_spot_gex.json",
                    "get_greek_exposure_by_strike",
                    {"ticker": ticker, "date": date_str})
        await _save(f"{ticker}_net_prem_ticks.json",
                    "get_flow_per_strike",
                    {"ticker": ticker, "date": date_str})

        # Endpoints that do NOT support date= — fetch current snapshot
        # In daily capture mode these reflect that day's close; in historical mode
        # they reflect today's data (less accurate but still useful for contract structure)
        await _save(f"{ticker}_darkpool.json",
                    "get_dark_pool_trades",
                    {"ticker_symbol": ticker, "limit": 100})
        await _save(f"{ticker}_option_contracts.json",
                    "get_options_chain",
                    {"ticker": ticker, "limit": 100})

        # Technicals — no date param needed (returns recent series)
        await _save(f"{ticker}_technicals_RSI.json",
                    "get_extended_technical_indicator",
                    {"ticker": ticker, "function": "RSI", "interval": "daily"})
        await _save(f"{ticker}_technicals_MACD.json",
                    "get_extended_technical_indicator",
                    {"ticker": ticker, "function": "MACD", "interval": "daily"})

        logger.info("%s: saved %s", trade_date, ticker)

    for i, ticker in enumerate(tickers):
        await _fetch_ticker(ticker)
        if i < len(tickers) - 1 and ticker_delay > 0:
            await asyncio.sleep(ticker_delay)

    logger.info("%s: complete — %d tickers → %s", trade_date, len(tickers), day_dir)


async def _main(args: argparse.Namespace) -> None:
    tools_list = await load_uw_tools()
    tbn = tools_by_name(tools_list)

    required = {
        "get_market_tide", "get_flow_alerts",
        "get_greek_exposure_by_strike", "get_dark_pool_trades",
        "get_flow_per_strike", "get_options_chain",
        "get_extended_technical_indicator",
    }
    missing = required - set(tbn.keys())
    if missing:
        logger.error("UW MCP missing tools: %s", missing)
        sys.exit(1)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    seeds = args.tickers or []
    sem = asyncio.Semaphore(_SEM_LIMIT)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    days = _trading_days(start, end)

    logger.info("Fetching %d trading days (%s → %s)", len(days), start, end)
    if args.historical_only:
        logger.info("historical-only mode: GEX/tide/flow-per-strike use date=; alerts/darkpool/chain are SKIPPED")
    else:
        logger.info("daily-capture mode: GEX/tide use date=; alerts/darkpool/chain are today's snapshot")

    for i, d in enumerate(days):
        await _fetch_day(tbn, d, out_dir, seeds, args.min_premium, args.max_tickers,
                         sem, args.ticker_delay, args.historical_only)
        if i < len(days) - 1 and args.date_delay > 0:
            await asyncio.sleep(args.date_delay)

    logger.info("Done → %s", out_dir)
    logger.info("Backtest: python scripts/run_backtest.py --fixtures %s --start %s --end %s --tickers %s",
                out_dir, start, end, " ".join(seeds) if seeds else "SPY")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Download UW data for backtest fixtures",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--start", required=True, metavar="YYYY-MM-DD")
    p.add_argument("--end", required=True, metavar="YYYY-MM-DD")
    p.add_argument("--out", default="data/history", metavar="DIR")
    p.add_argument("--tickers", nargs="*", metavar="TICKER",
                   help="Seed tickers always fetched (plus flow-alert discovered ones)")
    p.add_argument("--min-premium", type=int, default=_DEFAULT_MIN_PREMIUM, metavar="USD")
    p.add_argument("--max-tickers", type=int, default=_DEFAULT_MAX_TICKERS, metavar="N")
    p.add_argument("--date-delay", type=float, default=2.0, metavar="SEC")
    p.add_argument("--ticker-delay", type=float, default=0.5, metavar="SEC")
    p.add_argument(
        "--historical-only", action="store_true",
        help="Skip flow alerts/darkpool/options (only fetch endpoints that support date=). "
             "Useful for backfilling GEX history without polluting fixtures with today's alerts.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
