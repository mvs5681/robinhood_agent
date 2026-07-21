#!/usr/bin/env python3
"""
Daily UW snapshot capture — run at or after market close (4:30 PM ET).

Saves today's full UW snapshot into data/history/YYYY-MM-DD/ so the backtest
harness can replay it after 30+ days of captures.

The live agent (run_live.py) runs this automatically via CaptureLoop.
Use this script when you want to trigger a capture manually, e.g.:

    python scripts/capture_today.py

Or as a Docker one-shot after market close:
    docker run --rm --env-file .env -v $(pwd)/data:/app/data \\
        ghcr.io/<repo>:latest python scripts/capture_today.py

Token: UW_API_TOKEN must be set in .env or the environment.
HISTORY_DIR defaults to data/history; override via env var.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from trader.live.capture_loop import capture_day
from trader.uw.mcp_config import load_uw_tools, tools_by_name

logger = logging.getLogger(__name__)


async def _main() -> None:
    seeds_env = os.environ.get("TICKERS", "")
    seeds = [t.strip() for t in seeds_env.split(",") if t.strip()] if seeds_env else []
    out_dir = Path(os.environ.get("HISTORY_DIR", "data/history"))
    min_premium = int(os.environ.get("DISCOVERY_MIN_PREMIUM", "250000"))
    max_tickers = int(os.environ.get("MAX_DISCOVERED_TICKERS", "20"))

    today = date.today()
    if today.weekday() >= 5:
        logger.info("Today is a weekend (%s) — nothing to capture", today)
        return

    logger.info("Connecting to UW MCP…")
    tools_list = await load_uw_tools()
    tools = tools_by_name(tools_list)

    logger.info("Capturing today's snapshot → %s", today)
    await capture_day(tools, today, out_dir, seeds, min_premium, max_tickers)
    logger.info("Capture complete → %s", out_dir / today.isoformat())


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_main())
