#!/usr/bin/env python3
"""Live trading entrypoint.

Runs three async loops in a single process:
  1. GEXScanner  — hourly refresh of slow-moving per-ticker data
  2. FlowWatcher — polls flow alerts every 60 s, fires pipeline on hits
  3. HTTP server — approval UI on :8080 (GET /proposals, POST /approve/reject)

Configuration is entirely via environment variables (see .env.example):
    TICKERS           comma-separated list, e.g. "AAPL,SPY,QQQ"
    EXECUTION_MODE    propose_only | rh_approval | autonomous
    UW_API_TOKEN      Unusual Whales API token
    RH_ACCESS_TOKEN   Robinhood Bearer token (from scripts/auth_robinhood.py)
    RH_REFRESH_TOKEN  Robinhood refresh token
    RH_CLIENT_ID      Robinhood OAuth client ID
    RH_ACCOUNT_NUMBER Robinhood account number
    LOG_LEVEL         Python log level (default: INFO)
    TELEMETRY_LOG_FILE  Path to write JSON event log (optional)
    HTTP_PORT         Approval server port (default: 8080)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from decimal import Decimal
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
load_dotenv()

from trader.executor.executor import Executor
from trader.executor.schemas import ExecutionMode
from trader.live.approval_server import create_app
from trader.live.cache import GEXCache
from trader.live.proposals import ProposalStore
from trader.live.scanner import GEXScanner
from trader.live.watcher import FlowWatcher
from trader.rh.mcp_config import load_rh_tools
from trader.telemetry.logger import TelemetryLogger
from trader.uw.mcp_config import load_uw_tools

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _require(var: str) -> str:
    val = os.environ.get(var, "")
    if not val:
        raise RuntimeError(f"Required env var {var!r} is not set")
    return val


async def main() -> None:
    tickers = [t.strip().upper() for t in _require("TICKERS").split(",") if t.strip()]
    mode_str = os.environ.get("EXECUTION_MODE", "rh_approval").lower()
    mode = ExecutionMode(mode_str)
    account_number = os.environ.get("RH_ACCOUNT_NUMBER", "")
    port = int(os.environ.get("HTTP_PORT", "8080"))
    flow_min_premium = Decimal(os.environ.get("FLOW_MIN_PREMIUM", "100000"))

    logger.info("Starting live agent: tickers=%s mode=%s port=%d", tickers, mode.value, port)

    tel = TelemetryLogger(
        log_file=os.environ.get("TELEMETRY_LOG_FILE"),
    )

    # Load both MCP tool sets concurrently
    logger.info("Connecting to UW and RH MCP servers…")
    uw_tools_list, rh_tools_list = await asyncio.gather(
        load_uw_tools(),
        load_rh_tools() if mode != ExecutionMode.PROPOSE_ONLY else asyncio.coroutine(lambda: [])(),
    )
    uw_tools = {t.name: t for t in uw_tools_list}
    rh_tools = {t.name: t for t in rh_tools_list}
    logger.info("UW tools: %s", sorted(uw_tools))
    if rh_tools:
        logger.info("RH tools: %s", sorted(rh_tools))

    cache = GEXCache()
    proposal_store = ProposalStore()

    executor = Executor(
        mode=mode,
        account_number=account_number,
        rh_tools=rh_tools,
        quantity=int(os.environ.get("ORDER_QUANTITY", "1")),
    )

    scanner = GEXScanner(
        tickers=tickers,
        uw_tools=uw_tools,
        cache=cache,
        tel=tel,
    )

    watcher = FlowWatcher(
        tickers=tickers,
        uw_tools=uw_tools,
        cache=cache,
        proposal_store=proposal_store,
        execution_mode=mode,
        executor=executor,
        flow_min_premium=flow_min_premium,
        tel=tel,
    )

    app = create_app(proposal_store=proposal_store, executor=executor, tel=tel)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Approval server listening on :%d", port)

    try:
        await asyncio.gather(
            scanner.run(),
            watcher.run(),
        )
    finally:
        await runner.cleanup()
        tel.close()


if __name__ == "__main__":
    asyncio.run(main())
