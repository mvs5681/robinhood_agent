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
from trader.exits.monitor import ExitMonitor
from trader.live.approval_server import create_app
from trader.live.cache import GEXCache
from trader.live.exit_loop import ExitLoop
from trader.live.notifier import TelegramNotifier
from trader.live.position_store import PositionStore
from trader.live.proposals import ProposalStore
from trader.live.scanner import GEXScanner
from trader.live.telemetry_reader import TelemetryReader
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
    seed_tickers = [t.strip().upper() for t in os.environ.get("TICKERS", "").split(",") if t.strip()]
    mode_str = os.environ.get("EXECUTION_MODE", "rh_approval").lower()
    mode = ExecutionMode(mode_str)
    account_number = os.environ.get("RH_ACCOUNT_NUMBER", "")
    port = int(os.environ.get("HTTP_PORT", "8080"))
    flow_min_premium = Decimal(os.environ.get("FLOW_MIN_PREMIUM", "100000"))

    logger.info("Starting live agent: seed_tickers=%s mode=%s port=%d", seed_tickers, mode.value, port)

    telemetry_log_file = os.environ.get("TELEMETRY_LOG_FILE")
    tel = TelemetryLogger(
        log_file=telemetry_log_file,
    )
    tel_reader = TelemetryReader(log_file=telemetry_log_file)

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    public_url = os.environ.get("PUBLIC_URL", "")
    notifier: TelegramNotifier | None = None
    if tg_token and tg_chat_id:
        notifier = TelegramNotifier(tg_token, tg_chat_id, public_url)
        logger.info("Telegram notifications enabled (chat_id=%s)", tg_chat_id)
    else:
        logger.info("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — notifications disabled")

    # Load both MCP tool sets concurrently
    logger.info("Connecting to UW and RH MCP servers…")
    if mode != ExecutionMode.PROPOSE_ONLY:
        uw_tools_list, rh_tools_list = await asyncio.gather(load_uw_tools(), load_rh_tools())
    else:
        uw_tools_list = await load_uw_tools()
        rh_tools_list = []
    uw_tools = {t.name: t for t in uw_tools_list}
    rh_tools = {t.name: t for t in rh_tools_list}
    logger.info("UW tools: %s", sorted(uw_tools))
    if rh_tools:
        logger.info("RH tools: %s", sorted(rh_tools))

    cache = GEXCache()
    proposal_store = ProposalStore()
    position_store = PositionStore()

    max_trade_spend_raw = os.environ.get("MAX_TRADE_SPEND", "")
    max_trade_spend = Decimal(max_trade_spend_raw) if max_trade_spend_raw else None

    executor = Executor(
        mode=mode,
        account_number=account_number,
        rh_tools=rh_tools,
        quantity=int(os.environ.get("ORDER_QUANTITY", "1")),
        max_trade_spend=max_trade_spend,
        max_contracts=int(os.environ.get("MAX_CONTRACTS", "20")),
    )

    discovery_min_premium = Decimal(os.environ.get("DISCOVERY_MIN_PREMIUM", "500000"))
    max_discovered_tickers = int(os.environ.get("MAX_DISCOVERED_TICKERS", "20"))

    scanner = GEXScanner(
        uw_tools=uw_tools,
        cache=cache,
        seed_tickers=seed_tickers,
        min_discovery_premium=discovery_min_premium,
        max_discovered_tickers=max_discovered_tickers,
        tel=tel,
    )

    stop_loss_pct = float(os.environ.get("STOP_LOSS_PCT", "0.35"))
    dte_floor = int(os.environ.get("DTE_FLOOR", "7"))

    watcher = FlowWatcher(
        uw_tools=uw_tools,
        cache=cache,
        proposal_store=proposal_store,
        execution_mode=mode,
        executor=executor,
        flow_min_premium=flow_min_premium,
        tel=tel,
        notifier=notifier,
        position_store=position_store,
    )

    exit_loop = ExitLoop(
        rh_tools=rh_tools,
        position_store=position_store,
        account_number=account_number,
        execution_mode=mode,
        monitor=ExitMonitor(stop_loss_pct=stop_loss_pct, dte_floor=dte_floor),
        tel=tel,
        notifier=notifier,
    )

    app = create_app(
        proposal_store=proposal_store,
        executor=executor,
        tel=tel,
        telemetry_reader=tel_reader,
        cache=cache,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Approval server listening on :%d", port)

    coroutines = [scanner.run(), watcher.run(), exit_loop.run()]
    if notifier:
        coroutines.append(notifier.run_poller(proposal_store, executor, tel, position_store))

    try:
        await asyncio.gather(*coroutines)
    finally:
        await runner.cleanup()
        tel.close()


if __name__ == "__main__":
    asyncio.run(main())
