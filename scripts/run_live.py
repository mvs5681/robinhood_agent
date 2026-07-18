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
import json
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
from trader.live.config import LiveConfig
from trader.live.exit_loop import ExitLoop
from trader.live.notifier import TelegramNotifier
from trader.live.order_manager import OrderLifecycleManager
from trader.live.position_store import PositionStore
from trader.live.proposals import ProposalStore
from trader.live.reconciler import reconcile_positions
from trader.live.scanner import GEXScanner
from trader.live.telemetry_reader import TelemetryReader
from trader.live.watcher import FlowWatcher
from trader.rh.mcp_config import load_rh_tools, refresh_rh_token
from trader.risk.engine import RiskEngine
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


def _unwrap_mcp(result: object) -> object:
    """MCP tools return [{'type': 'text', 'text': '<json>'}]. Unwrap to plain object."""
    if isinstance(result, list) and result and isinstance(result[0], dict) and "text" in result[0]:
        try:
            return json.loads(result[0]["text"])
        except Exception:
            pass
    return result


async def _validate_account(rh_tools: dict, account_number: str) -> None:
    """Assert account is agentic-enabled with options level 2+. Raises on failure."""
    raw = await rh_tools["get_accounts"].ainvoke({})
    result = _unwrap_mcp(raw)
    accounts: list = []
    if isinstance(result, dict):
        inner = result.get("data", result)
        accounts = inner.get("accounts", inner.get("results", [inner])) if isinstance(inner, dict) else []
    elif isinstance(result, list):
        accounts = result

    target = None
    for acct in accounts:
        if isinstance(acct, dict) and acct.get("account_number") == account_number:
            target = acct
            break

    if target is None:
        raise RuntimeError(
            f"Account {account_number!r} not found in get_accounts response. "
            "Check RH_ACCOUNT_NUMBER."
        )

    if not target.get("agentic_allowed", False):
        raise RuntimeError(
            f"Account {account_number!r} has agentic_allowed=False. "
            "Enable agentic trading in the Robinhood app before running."
        )

    option_level: str = target.get("option_level", "") or ""
    if option_level not in ("option_level_2", "option_level_3"):
        raise RuntimeError(
            f"Account {account_number!r} has option_level={option_level!r}. "
            "Options level 2 or 3 required — enroll in the Robinhood app."
        )

    logger.info(
        "Account validated: %s agentic_allowed=True option_level=%s",
        account_number, option_level,
    )


async def main() -> None:
    mode_str = os.environ.get("EXECUTION_MODE", "rh_approval").lower()
    mode = ExecutionMode(mode_str)
    account_number = os.environ.get("RH_ACCOUNT_NUMBER", "")
    port = int(os.environ.get("HTTP_PORT", "8080"))

    # Runtime-tunable settings: env defaults, overridden by dashboard edits
    # persisted on the mounted volume (survive restarts)
    config = LiveConfig.from_env(os.environ.get("LIVE_CONFIG_FILE", "logs/live_config.json"))

    logger.info("Starting live agent: mode=%s port=%d config=%s", mode.value, port, config.to_dict())

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

    # Always exchange refresh token for a fresh access token before connecting.
    # RH_ACCESS_TOKEN in .env may be stale; the token file on the mounted volume
    # is preferred and updated after each refresh.
    if mode != ExecutionMode.PROPOSE_ONLY:
        logger.info("Refreshing RH OAuth token…")
        await refresh_rh_token()

    # Load both MCP tool sets concurrently
    logger.info("Connecting to UW and RH MCP servers…")
    if mode != ExecutionMode.PROPOSE_ONLY:
        uw_tools_list, rh_tools_list = await asyncio.gather(load_uw_tools(), load_rh_tools())
    else:
        uw_tools_list = await load_uw_tools()
        rh_tools_list = []
    uw_tools = {t.name: t for t in uw_tools_list}
    # rh_tools is a shared mutable dict — Executor and ExitLoop both hold a reference.
    # reload_rh_tools() updates it in-place on mid-session 401s.
    rh_tools: dict = {t.name: t for t in rh_tools_list}
    logger.info("UW tools: %s", sorted(uw_tools))
    if rh_tools:
        logger.info("RH tools: %s", sorted(rh_tools))

    # Validate account capabilities before accepting any traffic
    if account_number and rh_tools:
        await _validate_account(rh_tools, account_number)

    cache = GEXCache()
    proposal_store = ProposalStore()
    position_store = PositionStore()
    # Position cap reads live from PositionStore so exits free up slots
    risk_engine = RiskEngine(open_positions_fn=lambda: position_store.count)

    # Order lifecycle: fill promotion, reprice-toward-ask, give-up cancel
    order_manager: OrderLifecycleManager | None = None
    if mode != ExecutionMode.PROPOSE_ONLY and rh_tools:
        order_manager = OrderLifecycleManager(
            rh_tools=rh_tools,
            position_store=position_store,
            account_number=account_number,
            notifier=notifier,
            tel=tel,
            max_premium_per_contract=risk_engine.params.max_premium_per_trade,
        )

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

    scanner = GEXScanner(
        uw_tools=uw_tools,
        cache=cache,
        tel=tel,
        config=config,
    )

    watcher = FlowWatcher(
        uw_tools=uw_tools,
        cache=cache,
        proposal_store=proposal_store,
        execution_mode=mode,
        executor=executor,
        flow_min_premium=config.flow_min_premium,
        tel=tel,
        notifier=notifier,
        position_store=position_store,
        risk_engine=risk_engine,
        config=config,
        order_manager=order_manager,
    )

    exit_loop = ExitLoop(
        rh_tools=rh_tools,
        position_store=position_store,
        account_number=account_number,
        execution_mode=mode,
        monitor=ExitMonitor(stop_loss_pct=config.stop_loss_pct, dte_floor=config.dte_floor),
        tel=tel,
        notifier=notifier,
        risk_engine=risk_engine,
        config=config,
    )

    dashboard_token = os.environ.get("DASHBOARD_TOKEN", "")
    if dashboard_token:
        logger.info("Dashboard token auth enabled — access via /?token=<DASHBOARD_TOKEN>")
    app = create_app(
        proposal_store=proposal_store,
        executor=executor,
        tel=tel,
        telemetry_reader=tel_reader,
        cache=cache,
        dashboard_token=dashboard_token,
        position_store=position_store,
        config=config,
        order_manager=order_manager,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Approval server listening on :%d", port)

    # Reconcile any open positions from before this container started
    if mode != ExecutionMode.PROPOSE_ONLY and account_number and rh_tools:
        await reconcile_positions(rh_tools, position_store, account_number)

    # Adopt agentic orders still working from before the restart, so a fill
    # after the restart still becomes a monitored position
    if order_manager is not None and account_number:
        await order_manager.adopt_working_orders()

    # Telegram startup check — sends a test Approve/Reject message so the user
    # can confirm the bot is reachable and interactive before any real trades fire.
    if notifier:
        await notifier.send_startup_check()

    coroutines = [scanner.run(), watcher.run(), exit_loop.run()]
    if order_manager is not None:
        coroutines.append(order_manager.run())
    if notifier:
        coroutines.append(notifier.run_poller(proposal_store, executor, tel,
                                              position_store, order_manager))

    try:
        await asyncio.gather(*coroutines)
    finally:
        await runner.cleanup()
        tel.close()


if __name__ == "__main__":
    asyncio.run(main())
