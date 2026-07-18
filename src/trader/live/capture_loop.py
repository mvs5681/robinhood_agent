"""
Daily market-close snapshot — saves UW data to HISTORY_DIR/YYYY-MM-DD/
so the backtest harness can replay it once enough days have accumulated.

Runs as a background coroutine inside the live agent (run_live.py).
Fires once per trading day at 4:30 PM ET (30 min after US market close).

What gets captured:
  market_tide.json         — net call/put premium, market sentiment
  flow_alerts.json         — unusual flow from the current session
  {TICKER}_spot_gex.json   — gamma exposure by strike
  {TICKER}_darkpool.json   — dark pool prints
  {TICKER}_net_prem_ticks.json — net premium per strike
  {TICKER}_option_contracts.json — live options chain (DTE 21-30)
  {TICKER}_technicals_RSI.json  — RSI daily
  {TICKER}_technicals_MACD.json — MACD daily

After 30+ days of captures you can run:
    python scripts/run_backtest.py --fixtures data/history ...
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    # Fallback if tzdata not installed — use fixed UTC-4 (EDT)
    from datetime import timezone
    _ET = timezone(timedelta(hours=-4))  # type: ignore[assignment]

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

_CAPTURE_TIME = time(16, 30)   # 4:30 PM ET — 30 min after market close
_SEM_LIMIT = 3
_DEFAULT_MIN_PREMIUM = 250_000
_DEFAULT_MAX_TICKERS = 20


def _seconds_until_next_capture() -> float:
    """Return seconds until the next 4:30 PM ET on a weekday."""
    now = datetime.now(_ET)
    target = now.replace(hour=16, minute=30, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    # Skip weekends
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return max((target - now).total_seconds(), 1.0)


async def _uw_call(tool: "BaseTool", kwargs: dict) -> dict:
    try:
        return await tool.ainvoke(kwargs)
    except Exception as exc:
        logger.warning("UW capture: %s %s → %s", tool.name, kwargs, exc)
        return {"data": []}


async def capture_day(
    tools: dict[str, "BaseTool"],
    trade_date: date,
    history_dir: Path,
    seeds: list[str],
    min_premium: int = _DEFAULT_MIN_PREMIUM,
    max_tickers: int = _DEFAULT_MAX_TICKERS,
) -> None:
    """
    Save today's UW snapshot into history_dir/YYYY-MM-DD/.
    Skips silently if the directory already has a market_tide.json.
    This function is also importable by capture_today.py.
    """
    day_dir = history_dir / trade_date.isoformat()
    if (day_dir / "market_tide.json").exists():
        logger.info("CaptureLoop: %s already captured — skipping", trade_date)
        return

    day_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(_SEM_LIMIT)
    date_str = trade_date.isoformat()
    logger.info("CaptureLoop: capturing %s", trade_date)

    # ── Market-wide ──────────────────────────────────────────────────────────
    async with sem:
        tide_raw = await _uw_call(tools["get_market_tide"], {"date": date_str})
    (day_dir / "market_tide.json").write_text(json.dumps(tide_raw))

    async with sem:
        alerts_raw = await _uw_call(
            tools["get_flow_alerts"],
            {"limit": 200, "min_premium": str(min_premium)},
        )
    (day_dir / "flow_alerts.json").write_text(json.dumps(alerts_raw))

    # Discover tickers from flow alerts
    from trader.uw.validators import parse_flow_alerts
    discovered: list[str] = []
    try:
        alerts = parse_flow_alerts(alerts_raw)
        from collections import defaultdict
        from decimal import Decimal
        prem_by: dict[str, Decimal] = defaultdict(Decimal)
        for a in alerts:
            prem_by[a.ticker] += a.total_premium
        ranked = sorted(prem_by, key=lambda t: prem_by[t], reverse=True)
        discovered = [t for t in ranked if prem_by[t] >= Decimal(min_premium)]
    except Exception as exc:
        logger.warning("CaptureLoop: flow alert discovery failed: %s", exc)

    # Merge seeds + discovered, cap count
    seen: set[str] = set()
    tickers: list[str] = []
    for t in seeds + discovered:
        if t not in seen:
            seen.add(t)
            tickers.append(t)
    tickers = tickers[:max_tickers]

    if not tickers:
        logger.warning("CaptureLoop: no tickers to capture on %s", trade_date)
        return

    logger.info("CaptureLoop: %s — tickers: %s", trade_date, tickers)

    # ── Per-ticker ───────────────────────────────────────────────────────────
    async def _save(filename: str, tool_name: str, kwargs: dict) -> None:
        async with sem:
            raw = await _uw_call(tools[tool_name], kwargs)
        (day_dir / filename).write_text(json.dumps(raw))

    ticker_tasks = []
    for ticker in tickers:
        ticker_tasks.append(_capture_ticker(tools, ticker, day_dir, date_str, sem))
    await asyncio.gather(*ticker_tasks, return_exceptions=True)

    logger.info("CaptureLoop: %s complete → %s", trade_date, day_dir)


async def _capture_ticker(
    tools: dict[str, "BaseTool"],
    ticker: str,
    day_dir: Path,
    date_str: str,
    sem: asyncio.Semaphore,
) -> None:
    async def _save(filename: str, tool_name: str, kwargs: dict) -> None:
        async with sem:
            raw = await _uw_call(tools[tool_name], kwargs)
        (day_dir / filename).write_text(json.dumps(raw))

    await _save(f"{ticker}_spot_gex.json", "get_greek_exposure_by_strike",
                {"ticker": ticker, "date": date_str})
    await _save(f"{ticker}_darkpool.json", "get_dark_pool_trades",
                {"ticker_symbol": ticker, "limit": 100})
    await _save(f"{ticker}_net_prem_ticks.json", "get_flow_per_strike",
                {"ticker": ticker, "date": date_str})
    await _save(f"{ticker}_option_contracts.json", "get_options_chain",
                {"ticker": ticker, "limit": 50})

    for fn in ("RSI", "MACD"):
        await _save(
            f"{ticker}_technicals_{fn}.json",
            "get_extended_technical_indicator",
            {"ticker": ticker, "function": fn, "interval": "daily"},
        )

    logger.debug("CaptureLoop: %s done", ticker)


class CaptureLoop:
    """
    Background coroutine that fires once per trading day at 4:30 PM ET.
    Add to the asyncio.gather() in run_live.py to enable automatic capture.
    """

    def __init__(
        self,
        uw_tools: dict[str, "BaseTool"],
        history_dir: str | Path,
        seeds: list[str] | None = None,
        min_premium: int = _DEFAULT_MIN_PREMIUM,
        max_tickers: int = _DEFAULT_MAX_TICKERS,
    ) -> None:
        self._tools = uw_tools
        self._history_dir = Path(history_dir)
        self._seeds = list(seeds or [])
        self._min_premium = min_premium
        self._max_tickers = max_tickers

    async def run(self) -> None:
        while True:
            sleep_secs = _seconds_until_next_capture()
            next_time = datetime.now(_ET) + timedelta(seconds=sleep_secs)
            logger.info(
                "CaptureLoop: next capture at %s ET (in %.0f min)",
                next_time.strftime("%Y-%m-%d %H:%M"), sleep_secs / 60,
            )
            await asyncio.sleep(sleep_secs)

            today = date.today()
            if today.weekday() >= 5:
                logger.info("CaptureLoop: weekend — skipping")
                continue

            try:
                await capture_day(
                    self._tools,
                    today,
                    self._history_dir,
                    self._seeds,
                    self._min_premium,
                    self._max_tickers,
                )
            except Exception as exc:
                logger.error("CaptureLoop: capture failed for %s: %s", today, exc)
