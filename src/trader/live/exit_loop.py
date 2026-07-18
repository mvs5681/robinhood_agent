"""Exit monitor loop — polls open positions every 60 s and auto-sells on trigger.

Exit conditions (checked in priority order):
  1. Profit target  — underlying spot >= GEX gamma wall stored at entry
  2. Stop loss      — option premium dropped >= stop_loss_pct from entry
  3. DTE stop       — days-to-expiry <= dte_floor (default 7)

Exits are always autonomous regardless of EXECUTION_MODE — no approval gate.
In propose_only mode the sell is logged but not placed.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import NAMESPACE_OID, uuid5

from trader.exits.monitor import ExitMonitor
from trader.exits.schemas import ExitReason, ExitSignal, Position
from trader.executor.schemas import ExecutionMode
from trader.rh.mcp_config import rh_call
from trader.rh.ticks import round_price_to_tick

from .market_hours import is_market_hours

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool
    from trader.live.config import LiveConfig
    from trader.live.notifier import TelegramNotifier
    from trader.live.position_store import PositionStore
    from trader.risk.engine import RiskEngine
    from trader.telemetry.logger import TelemetryLogger

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 60
_IDLE_SLEEP = 120


def _extract_order_id(result: object) -> str | None:
    if isinstance(result, dict):
        inner = result.get("data", result)
        if isinstance(inner, dict) and isinstance(inner.get("order"), dict):
            inner = inner["order"]  # place_option_order: {"data": {"order": {"id": ...}}}
        if isinstance(inner, dict):
            return inner.get("id") or inner.get("order_id")
    return None


def _extract_items(result: object) -> list:
    """Pull a result list out of an RH payload, handling {"data": {...}} nesting."""
    if isinstance(result, dict):
        inner = result.get("data", result)
        if isinstance(inner, dict):
            inner = inner.get("results", inner.get("instruments", []))
        return inner if isinstance(inner, list) else []
    if isinstance(result, list):
        return result
    return []


class ExitLoop:
    """
    Async polling loop that evaluates open positions and fires sell_to_close
    orders automatically when an exit condition triggers.
    """

    def __init__(
        self,
        rh_tools: dict[str, "BaseTool"],
        position_store: "PositionStore",
        account_number: str,
        execution_mode: ExecutionMode,
        monitor: ExitMonitor | None = None,
        tel: "TelemetryLogger | None" = None,
        notifier: "TelegramNotifier | None" = None,
        poll_interval: int = _POLL_INTERVAL,
        risk_engine: "RiskEngine | None" = None,
        config: "LiveConfig | None" = None,
    ) -> None:
        self._rh_tools = rh_tools
        self._store = position_store
        self._account_number = account_number
        self._mode = execution_mode
        self._monitor = monitor or ExitMonitor()
        self._tel = tel
        self._notifier = notifier
        self._poll_interval = poll_interval
        self._risk_engine = risk_engine
        self._config = config

    async def run(self) -> None:
        logger.info(
            "ExitLoop started — stop_loss=%.0f%% dte_floor=%d poll=%ds",
            self._monitor.stop_loss_pct * 100,
            self._monitor.dte_floor,
            self._poll_interval,
        )
        while True:
            if not is_market_hours():
                await asyncio.sleep(_IDLE_SLEEP)
                continue
            await asyncio.sleep(self._poll_interval)
            try:
                await self._tick()
            except Exception as exc:
                logger.error("ExitLoop tick error: %s", exc)

    # ------------------------------------------------------------------
    # Polling tick
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        if self._config is not None:
            self._monitor.stop_loss_pct = self._config.stop_loss_pct
            self._monitor.dte_floor = self._config.dte_floor
        positions = await self._store.all()
        if not positions:
            return

        logger.debug("ExitLoop: evaluating %d open position(s)", len(positions))

        tickers = list({p.ticker for p in positions})
        prices = await self._batch_equity_prices(tickers)

        for pos in positions:
            try:
                await self._evaluate(pos, prices)
            except Exception as exc:
                logger.error("ExitLoop evaluate %s: %s", pos.ticker, exc)

    async def _evaluate(self, pos: Position, prices: dict[str, Decimal]) -> None:
        spot = prices.get(pos.ticker)
        if spot is None:
            logger.debug("ExitLoop: no spot for %s", pos.ticker)
            return

        premium, dte = await self._option_mid_and_dte(pos)
        if premium is None:
            logger.debug("ExitLoop: no option quote for %s", pos.ticker)
            return

        signal = self._monitor.evaluate(pos, spot, premium, dte)
        if signal:
            logger.info(
                "Exit triggered %s reason=%s pnl=%.1f%% spot=%s premium=%s dte=%d",
                pos.ticker, signal.reason.value, signal.pnl_pct * 100, spot, premium, dte,
            )
            await self._execute_exit(pos, signal)

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    async def _execute_exit(self, pos: Position, signal: ExitSignal) -> None:
        t0 = _time.monotonic()
        order_id: str | None = None
        dry_run = self._mode == ExecutionMode.PROPOSE_ONLY or not self._rh_tools

        if dry_run:
            logger.info(
                "PROPOSE_ONLY exit %s reason=%s pnl=%.1f%%",
                pos.ticker, signal.reason.value, signal.pnl_pct * 100,
            )
        else:
            try:
                instrument_id = pos.option_instrument_id or await self._resolve_instrument_id(pos)
                price = round_price_to_tick(
                    self._exit_limit_price(signal),
                    await self._instrument_min_ticks(instrument_id),
                )
                params = {
                    "account_number": self._account_number,
                    "quantity": str(pos.quantity),
                    "legs": [{
                        "option_id": instrument_id,
                        "side": "sell",
                        "position_effect": "close",
                    }],
                    "type": "limit",
                    "time_in_force": "gfd",
                    "price": f"{float(price):.2f}",
                    # Stable per position+reason: a retry after a transient
                    # failure (position stays in the store, next tick retries)
                    # is idempotent instead of double-selling
                    "ref_id": str(uuid5(NAMESPACE_OID, f"{pos.position_id}:{signal.reason.value}")),
                }
                result = await rh_call(self._rh_tools, "place_option_order", params)
                order_id = _extract_order_id(result)
                logger.info(
                    "EXIT placed %s reason=%s order_id=%s pnl=%.1f%%",
                    pos.ticker, signal.reason.value, order_id, signal.pnl_pct * 100,
                )
            except Exception as exc:
                logger.error("Exit order failed %s: %s", pos.ticker, exc)
                return  # keep position in store — retry next tick

        await self._store.remove(pos.position_id)

        if self._risk_engine is not None and not dry_run:
            realized = (signal.current_premium - pos.entry_premium) * 100 * pos.quantity
            self._risk_engine.record_pnl(realized)
            if self._risk_engine.kill_switch_active:
                logger.warning("Daily loss kill-switch tripped — new entries blocked")

        ms = round((_time.monotonic() - t0) * 1000, 1)
        if self._tel:
            self._tel.exit_signal(
                ticker=pos.ticker,
                position_id=pos.position_id,
                reason=signal.reason.value,
                pnl_pct=signal.pnl_pct,
                dte_remaining=signal.dte_remaining,
                entry_premium=float(pos.entry_premium),
                current_premium=float(signal.current_premium),
                duration_ms=ms,
            )

        if self._notifier:
            await self._notifier.notify_exit(signal, order_id)

    async def _instrument_min_ticks(self, instrument_id: str) -> dict | None:
        """Fetch the instrument's tick-grid rule; None falls back to pennies."""
        try:
            result = await rh_call(self._rh_tools, "get_option_instruments",
                                   {"ids": instrument_id})
            items = _extract_items(result)
            if items and isinstance(items[0], dict) and isinstance(items[0].get("min_ticks"), dict):
                return items[0]["min_ticks"]
        except Exception as exc:
            logger.warning("min_ticks lookup failed for %s: %s", instrument_id, exc)
        return None

    def _exit_limit_price(self, signal: ExitSignal) -> Decimal:
        price = signal.current_premium
        if signal.reason == ExitReason.STOP_LOSS:
            # Slightly below mid to improve fill probability under stress
            return max(price * Decimal("0.95"), Decimal("0.01"))
        return price

    # ------------------------------------------------------------------
    # Price helpers
    # ------------------------------------------------------------------

    async def _batch_equity_prices(self, tickers: list[str]) -> dict[str, Decimal]:
        if not self._rh_tools or "get_equity_quotes" not in self._rh_tools:
            return {}
        try:
            result = await rh_call(self._rh_tools, "get_equity_quotes", {"symbols": tickers})
            return self._parse_equity_quotes(result, tickers)
        except Exception as exc:
            logger.warning("get_equity_quotes failed: %s", exc)
            return {}

    def _parse_equity_quotes(self, result: object, tickers: list[str]) -> dict[str, Decimal]:
        prices: dict[str, Decimal] = {}
        items = _extract_items(result)
        if not items and isinstance(result, dict):
            inner = result.get("data", result)
            if isinstance(inner, dict):
                for t in tickers:
                    v = inner.get(t) or inner.get(t.lower())
                    if v is not None:
                        try:
                            prices[t] = Decimal(str(v))
                        except Exception:
                            pass
                return prices
        for item in items:
            if not isinstance(item, dict):
                continue
            # get_equity_quotes nests fields under "quote": results[].quote.last_trade_price
            q = item.get("quote") if isinstance(item.get("quote"), dict) else item
            symbol = (q.get("symbol") or q.get("ticker") or "").upper()
            raw = (
                q.get("last_trade_price")
                or q.get("ask_price")
                or q.get("bid_price")
                or q.get("price")
            )
            if symbol and raw:
                try:
                    prices[symbol] = Decimal(str(raw))
                except Exception:
                    pass
        return prices

    async def _option_mid_and_dte(self, pos: Position) -> tuple[Decimal | None, int]:
        dte = max((pos.contract.expiry - date.today()).days, 0)
        if not self._rh_tools or "get_option_quotes" not in self._rh_tools:
            return None, dte
        try:
            instrument_id = pos.option_instrument_id or await self._resolve_instrument_id(pos)
            result = await rh_call(self._rh_tools, "get_option_quotes",
                                   {"instrument_ids": [instrument_id]})
            mid = self._parse_option_mid(result)
            return mid, dte
        except Exception as exc:
            logger.warning("get_option_quotes failed %s: %s", pos.ticker, exc)
            return None, dte

    def _parse_option_mid(self, result: object) -> Decimal | None:
        items = _extract_items(result)
        if not items and isinstance(result, dict):
            inner = result.get("data", result)
            items = [inner] if isinstance(inner, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            # get_option_quotes nests fields under "quote": results[].quote.mark_price
            q = item.get("quote") if isinstance(item.get("quote"), dict) else item
            mid = q.get("mid_price") or q.get("mark_price")
            if mid is not None:
                try:
                    return Decimal(str(mid))
                except Exception:
                    pass
            bid = q.get("bid_price")
            ask = q.get("ask_price")
            if bid is not None and ask is not None:
                try:
                    return (Decimal(str(bid)) + Decimal(str(ask))) / 2
                except Exception:
                    pass
        return None

    async def _resolve_instrument_id(self, pos: Position) -> str:
        result = await rh_call(self._rh_tools, "get_option_instruments", {
            "chain_symbol": pos.contract.ticker,
            "expiration_dates": pos.contract.expiry.isoformat(),
            "strike_price": f"{float(pos.contract.strike):.4f}",
            "type": pos.contract.type,
            "state": "active",
        })
        instruments = _extract_items(result)
        if not instruments or not isinstance(instruments[0], dict) or "id" not in instruments[0]:
            raise ValueError(
                f"No active instrument: {pos.contract.ticker} "
                f"{pos.contract.expiry} {pos.contract.strike} {pos.contract.type} "
                f"(response: {str(result)[:200]})"
            )
        return instruments[0]["id"]
