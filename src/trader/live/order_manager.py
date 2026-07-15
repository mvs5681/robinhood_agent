"""Order lifecycle manager — fill tracking, repricing, and give-up for buys.

Placement was previously fire-and-forget: the executor placed a GFD limit at
mid and the position was tracked immediately, whether or not the order ever
filled. This module owns the gap between "order placed" and "position open":

  track()  — register a placed buy order (called instead of adding the
             position to PositionStore directly)
  run()    — poll working orders every _POLL_INTERVAL seconds:
               filled            → promote to PositionStore + notify
               terminal state    → drop (promote any partial fill)
               unfilled too long → cancel, then re-place stepping the price
                                   toward the ask (capped at the risk
                                   engine's per-contract premium cap)
               past give-up age  → cancel and drop
  adopt_working_orders() — at startup, pick up agentic buy orders that were
             working when the previous container stopped (reconciliation only
             sees positions, so a pre-restart order that fills post-restart
             was previously unmonitored).

Repricing is an explicit cancel → confirm cancelled → place sequence: the RH
MCP toolset exposes cancel_option_order but not replace_option_order. The
two-phase flow also handles the fill/cancel race — if the order fills before
the cancel lands, the next poll sees the fill and promotes it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

from trader.exits.schemas import Position
from trader.rh.mcp_config import rh_call
from trader.rh.ticks import round_price_to_tick
from trader.uw.schemas import OptionContract

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool
    from trader.executor.schemas import OrderResult
    from trader.scoring.schemas import CandidateSignal
    from trader.telemetry.logger import TelemetryLogger
    from .notifier import TelegramNotifier
    from .position_store import PositionStore

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 20        # seconds between order-state polls
_IDLE_SLEEP = 60           # seconds to sleep when nothing is tracked
_REPRICE_AFTER = 120       # seconds unfilled before stepping the price
_GIVE_UP_AFTER = 600       # seconds unfilled before cancelling for good
_MAX_REPLACEMENTS = 3

# Order states that end the lifecycle (fills are handled separately)
_TERMINAL_STATES = frozenset({"cancelled", "rejected", "failed", "voided", "expired"})
_WORKING_STATES = frozenset({"queued", "unconfirmed", "confirmed", "partially_filled",
                             "pending_cancelled"})


@dataclass
class WorkingOrder:
    order_id: str
    contract: OptionContract
    quantity: int
    price: Decimal                       # current limit price
    candidate: "CandidateSignal | None"  # None for orders adopted at startup
    option_id: str | None = None         # filled from the order's leg on first poll
    placed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_action_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    attempts: int = 0                    # replacements so far
    cancelling: bool = False             # cancel requested, awaiting confirmation
    giving_up: bool = False              # cancel is final — do not re-place
    min_ticks: dict | None = None        # instrument tick grid, fetched lazily

    @property
    def ticker(self) -> str:
        return self.contract.ticker


def _order_payload(result: object) -> dict:
    """Extract the order dict from a get_option_orders / place response."""
    if not isinstance(result, dict):
        return {}
    inner = result.get("data", result)
    if not isinstance(inner, dict):
        return {}
    if isinstance(inner.get("order"), dict):
        return inner["order"]
    orders = inner.get("orders")
    if isinstance(orders, list) and orders and isinstance(orders[0], dict):
        return orders[0]
    return {}


def _list_orders(result: object) -> list[dict]:
    if not isinstance(result, dict):
        return []
    inner = result.get("data", result)
    if isinstance(inner, dict):
        orders = inner.get("orders", [])
        return [o for o in orders if isinstance(o, dict)] if isinstance(orders, list) else []
    return []


def _parse_quote(result: object) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Return (bid, ask, mark) from a get_option_quotes payload."""
    if not isinstance(result, dict):
        return None, None, None
    inner = result.get("data", result)
    results = inner.get("results", []) if isinstance(inner, dict) else []
    for item in results:
        if not isinstance(item, dict):
            continue
        q = item.get("quote") if isinstance(item.get("quote"), dict) else item
        def dec(key: str) -> Decimal | None:
            v = q.get(key)
            try:
                return Decimal(str(v)) if v is not None else None
            except Exception:
                return None
        return dec("bid_price"), dec("ask_price"), dec("mark_price")
    return None, None, None


class OrderLifecycleManager:
    def __init__(
        self,
        rh_tools: dict[str, "BaseTool"],
        position_store: "PositionStore",
        account_number: str,
        notifier: "TelegramNotifier | None" = None,
        tel: "TelemetryLogger | None" = None,
        max_premium_per_contract: Decimal = Decimal("500"),
        poll_interval: int = _POLL_INTERVAL,
        reprice_after: int = _REPRICE_AFTER,
        give_up_after: int = _GIVE_UP_AFTER,
        max_replacements: int = _MAX_REPLACEMENTS,
    ) -> None:
        self._rh_tools = rh_tools
        self._store = position_store
        self._account_number = account_number
        self._notifier = notifier
        self._tel = tel
        self._price_cap = max_premium_per_contract / 100  # per-share limit cap
        self._poll_interval = poll_interval
        self._reprice_after = reprice_after
        self._give_up_after = give_up_after
        self._max_replacements = max_replacements
        self._orders: dict[str, WorkingOrder] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def track(self, candidate: "CandidateSignal", result: "OrderResult") -> None:
        """Register a freshly placed buy order for lifecycle management."""
        if not result.placed or not result.order_id:
            return
        contract = candidate.selected_contract
        if contract is None:
            return
        wo = WorkingOrder(
            order_id=result.order_id,
            contract=contract,
            quantity=result.request.quantity,
            price=result.request.limit_price or contract.mid,
            candidate=candidate,
        )
        async with self._lock:
            self._orders[wo.order_id] = wo
        logger.info("Tracking order %s %s x%d @ %s", wo.order_id, wo.ticker,
                    wo.quantity, wo.price)

    @property
    def working_count(self) -> int:
        return len(self._orders)

    # ------------------------------------------------------------------
    # Startup adoption
    # ------------------------------------------------------------------

    async def adopt_working_orders(self) -> int:
        """Pick up agentic buy orders still working from before a restart."""
        adopted = 0
        for state in ("confirmed", "queued"):
            try:
                result = await rh_call(self._rh_tools, "get_option_orders", {
                    "account_number": self._account_number,
                    "placed_agent": "agentic",
                    "state": state,
                })
            except Exception as exc:
                logger.error("adopt_working_orders(%s) failed: %s", state, exc)
                continue
            for order in _list_orders(result):
                wo = self._adopt_one(order)
                if wo is not None:
                    adopted += 1
        if adopted:
            logger.warning("Adopted %d working order(s) from before restart", adopted)
        return adopted

    def _adopt_one(self, order: dict) -> WorkingOrder | None:
        order_id = order.get("id")
        if not order_id or order_id in self._orders:
            return None
        legs = order.get("legs") or []
        leg = legs[0] if legs and isinstance(legs[0], dict) else {}
        if leg.get("side") != "buy" or leg.get("position_effect") != "open":
            return None
        try:
            price = Decimal(str(order.get("price")))
            contract = OptionContract(
                ticker=order.get("chain_symbol", ""),
                expiry=date.fromisoformat(leg["expiration_date"]),
                strike=Decimal(str(leg["strike_price"])),
                type=leg["option_type"],
                bid=Decimal("0"),
                ask=Decimal("0"),
                open_interest=0,
                volume=0,
            )
        except Exception as exc:
            logger.warning("Could not adopt order %s: %s", order_id, exc)
            return None
        wo = WorkingOrder(
            order_id=order_id,
            contract=contract,
            quantity=int(Decimal(str(order.get("quantity", "1")))),
            price=price,
            candidate=None,
            option_id=leg.get("option_id"),
        )
        self._orders[order_id] = wo
        logger.info("Adopted working order %s %s x%d @ %s",
                    order_id, wo.ticker, wo.quantity, wo.price)
        return wo

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        logger.info(
            "OrderLifecycleManager started — reprice_after=%ds give_up_after=%ds max_replacements=%d",
            self._reprice_after, self._give_up_after, self._max_replacements,
        )
        while True:
            if not self._orders:
                await asyncio.sleep(_IDLE_SLEEP)
                continue
            try:
                await self._tick()
            except Exception as exc:
                logger.error("OrderLifecycleManager tick error: %s", exc)
            await asyncio.sleep(self._poll_interval)

    async def _tick(self) -> None:
        for wo in list(self._orders.values()):
            try:
                await self._check_order(wo)
            except Exception as exc:
                logger.error("order %s check failed: %s", wo.order_id, exc)

    async def _check_order(self, wo: WorkingOrder) -> None:
        result = await rh_call(self._rh_tools, "get_option_orders", {
            "account_number": self._account_number,
            "order_id": wo.order_id,
        })
        order = _order_payload(result)
        if not order:
            logger.warning("order %s not found — dropping from tracking", wo.order_id)
            self._orders.pop(wo.order_id, None)
            return

        if wo.option_id is None:
            legs = order.get("legs") or []
            if legs and isinstance(legs[0], dict):
                wo.option_id = legs[0].get("option_id")

        state = str(order.get("state", "")).lower()
        processed = self._dec(order.get("processed_quantity")) or Decimal("0")

        if state == "filled":
            await self._promote(wo, order)
            return

        if state in _TERMINAL_STATES:
            if processed > 0:
                await self._promote(wo, order)   # partial fill before cancel
                return
            self._orders.pop(wo.order_id, None)
            if wo.cancelling and not wo.giving_up and wo.attempts < self._max_replacements:
                await self._place_replacement(wo)
            else:
                logger.info("order %s ended state=%s after %d attempt(s)",
                            wo.order_id, state, wo.attempts)
                if wo.giving_up or wo.cancelling:
                    await self._notify(
                        f"<b>Order cancelled unfilled</b>\n{wo.ticker} x{wo.quantity} "
                        f"after {wo.attempts + 1} attempt(s) — no position opened"
                    )
                self._emit(wo, event="ended", state=state)
            return

        if state not in _WORKING_STATES:
            logger.warning("order %s in unexpected state %r — leaving tracked",
                           wo.order_id, state)
            return

        if wo.cancelling:
            return  # waiting for the cancel to land

        age = (datetime.now(timezone.utc) - wo.placed_at).total_seconds()
        since_action = (datetime.now(timezone.utc) - wo.last_action_at).total_seconds()

        if age >= self._give_up_after:
            wo.giving_up = True
            await self._request_cancel(wo)
        elif since_action >= self._reprice_after and wo.attempts < self._max_replacements:
            await self._request_cancel(wo)
        # give-up exhausted replacements: order simply rides as GFD until close

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def _request_cancel(self, wo: WorkingOrder) -> None:
        try:
            await rh_call(self._rh_tools, "cancel_option_order", {
                "account_number": self._account_number,
                "order_id": wo.order_id,
            })
            wo.cancelling = True
            wo.last_action_at = datetime.now(timezone.utc)
            logger.info("cancel requested for order %s (%s, giving_up=%s)",
                        wo.order_id, wo.ticker, wo.giving_up)
        except Exception as exc:
            # Possibly already filled — the next poll resolves it either way
            logger.warning("cancel of %s failed (may have filled): %s", wo.order_id, exc)

    async def _place_replacement(self, wo: WorkingOrder) -> None:
        if wo.option_id is None:
            logger.error("cannot re-place %s — no option_id resolved", wo.ticker)
            return
        price = await self._next_price(wo)
        if price is None or price <= 0:
            logger.warning("no quote for %s — not re-placing", wo.ticker)
            return
        params = {
            "account_number": self._account_number,
            "quantity": str(wo.quantity),
            "legs": [{"option_id": wo.option_id, "side": "buy", "position_effect": "open"}],
            "type": "limit",
            "time_in_force": "gfd",
            "price": f"{float(price):.2f}",
            "ref_id": str(uuid4()),
        }
        try:
            result = await rh_call(self._rh_tools, "place_option_order", params)
        except Exception as exc:
            logger.error("re-place failed for %s: %s", wo.ticker, exc)
            return
        order = _order_payload(result)
        new_id = order.get("id")
        if not new_id:
            logger.error("re-place for %s returned no order id: %s",
                         wo.ticker, str(result)[:200])
            return
        wo.order_id = new_id
        wo.price = price
        wo.attempts += 1
        wo.cancelling = False
        wo.last_action_at = datetime.now(timezone.utc)
        self._orders[new_id] = wo
        logger.info("re-placed %s x%d @ %s (attempt %d/%d, order %s)",
                    wo.ticker, wo.quantity, price, wo.attempts,
                    self._max_replacements, new_id)
        self._emit(wo, event="repriced", state="confirmed")

    async def _next_price(self, wo: WorkingOrder) -> Decimal | None:
        """Ladder toward the ask: fresh mid → mid/ask midpoint → ask.

        Always capped at the risk engine's per-contract premium cap so a
        replacement can never exceed what the risk gate approved.
        """
        try:
            result = await rh_call(self._rh_tools, "get_option_quotes",
                                   {"instrument_ids": [wo.option_id]})
        except Exception as exc:
            logger.warning("quote fetch failed for %s: %s", wo.ticker, exc)
            return None
        bid, ask, mark = _parse_quote(result)
        mid = mark if mark is not None else (
            (bid + ask) / 2 if bid is not None and ask is not None else None
        )
        if mid is None:
            return None
        if wo.attempts == 0 or ask is None:
            price = mid
        elif wo.attempts == 1:
            price = (mid + ask) / 2
        else:
            price = ask
        price = min(price, self._price_cap)
        if wo.min_ticks is None:
            wo.min_ticks = await self._fetch_min_ticks(wo)
        return round_price_to_tick(price, wo.min_ticks)

    async def _fetch_min_ticks(self, wo: WorkingOrder) -> dict | None:
        try:
            result = await rh_call(self._rh_tools, "get_option_instruments",
                                   {"ids": wo.option_id})
            inner = result.get("data", result) if isinstance(result, dict) else {}
            items = inner.get("instruments", inner.get("results", [])) if isinstance(inner, dict) else []
            if items and isinstance(items[0], dict) and isinstance(items[0].get("min_ticks"), dict):
                return items[0]["min_ticks"]
        except Exception as exc:
            logger.warning("min_ticks lookup failed for %s: %s", wo.ticker, exc)
        return None

    # ------------------------------------------------------------------
    # Fill promotion
    # ------------------------------------------------------------------

    async def _promote(self, wo: WorkingOrder, order: dict) -> None:
        self._orders.pop(wo.order_id, None)
        qty = int(self._dec(order.get("processed_quantity")) or Decimal(wo.quantity))
        entry = self._avg_fill_premium(order, qty) or wo.price
        target = None
        if wo.candidate is not None and wo.candidate.gex_setup is not None:
            target = wo.candidate.gex_setup.target_level
        pos = Position(
            position_id=wo.order_id,
            ticker=wo.ticker,
            contract=wo.contract,
            entry_premium=entry,
            target_level=target,
            opened_at=datetime.now(timezone.utc),
            quantity=qty,
            option_instrument_id=wo.option_id,
        )
        await self._store.add(pos)
        logger.info("FILLED %s x%d @ %s — position tracked (%s)",
                    wo.ticker, qty, entry, wo.order_id)
        self._emit(wo, event="filled", state=str(order.get("state", "")))
        await self._notify(
            f"<b>Order filled</b>\n{wo.ticker} x{qty} @ <code>{entry}</code>\n"
            f"Position is now monitored for exits."
        )

    @staticmethod
    def _dec(v: object) -> Decimal | None:
        try:
            return Decimal(str(v)) if v is not None else None
        except Exception:
            return None

    def _avg_fill_premium(self, order: dict, qty: int) -> Decimal | None:
        """Per-share average fill: processed_premium is total dollars."""
        total = self._dec(order.get("processed_premium"))
        if total and total > 0 and qty > 0:
            return (total / (qty * 100)).quantize(Decimal("0.0001"))
        return None

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _emit(self, wo: WorkingOrder, *, event: str, state: str) -> None:
        if self._tel:
            self._tel.order_attempt(
                ticker=wo.ticker,
                mode="lifecycle",
                action=event,
                quantity=wo.quantity,
                limit_price=float(wo.price),
                placed=event in ("filled", "repriced"),
                order_id=wo.order_id,
                account_number=self._account_number or None,
                rejection_reason=None if event != "ended" else f"order_{state}",
                review_summary=None,
                duration_ms=None,
            )

    async def _notify(self, text: str) -> None:
        if self._notifier:
            try:
                await self._notifier.notify_text(text)
            except Exception as exc:
                logger.warning("order notification failed: %s", exc)
