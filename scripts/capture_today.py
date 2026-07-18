#!/usr/bin/env python3
"""
Daily snapshot capture — run at or after market close (4 PM ET) each trading day.

Saves today's full UW data into data/history/YYYY-MM-DD/ so the backtest
harness can replay it later. Run this daily and you'll have real historical
fixtures after 30-60 days.

Cron example (4:30 PM ET = 20:30 UTC, Mon-Fri):
    30 20 * * 1-5  cd /path/to/robinhood_agent && \
        .venv/bin/python3.12 scripts/capture_today.py >> logs/capture.log 2>&1

Or as a Docker one-shot after market close:
    docker run --rm --env-file .env ghcr.io/<repo>:latest \
        python scripts/capture_today.py

Token: UW_API_TOKEN must be set in .env or the environment.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Reuse fetch_history logic — just lock the date to today
from fetch_history import _fetch_day, _DEFAULT_MIN_PREMIUM, _DEFAULT_MAX_TICKERS
from trader.uw.mcp_config import load_uw_tools, tools_by_name

logger = logging.getLogger(__name__)


async def _main() -> None:
    import os
    seeds_env = os.environ.get("TICKERS", "")
    seeds = [t.strip() for t in seeds_env.split(",") if t.strip()] if seeds_env else []
    out_dir = Path(os.environ.get("HISTORY_DIR", "data/history"))
    min_premium = int(os.environ.get("DISCOVERY_MIN_PREMIUM", _DEFAULT_MIN_PREMIUM))
    max_tickers = int(os.environ.get("MAX_DISCOVERED_TICKERS", _DEFAULT_MAX_TICKERS))

    tools_list = await load_uw_tools()
    tbn = tools_by_name(tools_list)
    sem = asyncio.Semaphore(3)

    today = date.today()
    if today.weekday() >= 5:
        logger.info("Today is a weekend (%s) — nothing to capture", today)
        return

    logger.info("Capturing today's snapshot → %s", today)
    await _fetch_day(
        tbn, today, out_dir, seeds,
        min_premium, max_tickers, sem,
        ticker_delay=0.5,
        historical_only=False,
    )
    logger.info("Capture complete")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_main())
