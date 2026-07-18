"""Startup position reconciliation.

On container restart, the in-memory PositionStore is empty even if open
option positions exist in Robinhood. This module re-populates the store
from get_option_positions so the exit loop can manage them immediately.

Limitations of reconciled positions:
- entry_premium: taken from RH average_price (cost basis / 100 per share)
- target_level:  None — profit target disabled; stop-loss and DTE still active
- opened_at:     taken from RH created_at when available, else now()
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from trader.exits.schemas import Position
from trader.rh.mcp_config import rh_call
from trader.uw.schemas import OptionContract

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool
    from .position_store import PositionStore

logger = logging.getLogger(__name__)


def _unwrap_mcp(result: object) -> object:
    """MCP tools return [{'type': 'text', 'text': '<json>'}]. Unwrap to plain object."""
    if isinstance(result, list) and result and isinstance(result[0], dict) and "text" in result[0]:
        try:
            return json.loads(result[0]["text"])
        except Exception:
            pass
    return result


def _parse_positions(result: object) -> list[dict]:
    result = _unwrap_mcp(result)
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        inner = result.get("data", result)
        if isinstance(inner, dict):
            return inner.get("results", inner.get("positions", []))
        return result.get("results", result.get("positions", []))
    return []


def _to_position(item: dict) -> Position | None:
    """Convert one RH position dict to a Position. Returns None if required fields missing."""
    try:
        option_url: str = item.get("option", "") or ""
        instrument_id: str | None = item.get("option_id") or item.get("id") or None
        if not instrument_id and option_url:
            # Extract UUID from trailing path segment: .../instruments/<uuid>/
            parts = [p for p in option_url.rstrip("/").split("/") if p]
            instrument_id = parts[-1] if parts else None

        ticker: str = (item.get("chain_symbol") or item.get("symbol") or "").upper()
        if not ticker:
            logger.debug("Reconcile: skipping position with no ticker: %s", item)
            return None

        expiry_str: str = item.get("expiration_date") or item.get("expiry") or ""
        if not expiry_str:
            logger.debug("Reconcile: skipping %s — no expiration_date", ticker)
            return None
        expiry = date.fromisoformat(expiry_str)

        strike = Decimal(str(item.get("strike_price") or item.get("strike") or 0))
        if not strike:
            return None

        option_type: str = (item.get("option_type") or item.get("type") or "").lower()
        if option_type not in ("call", "put"):
            return None

        quantity_str = item.get("quantity") or item.get("contracts") or "0"
        quantity = int(Decimal(str(quantity_str)))
        if quantity <= 0:
            return None

        # RH average_price is per-contract (premium × 100) — convert to the
        # per-share premium that ExitMonitor compares against option mids
        avg_price_raw = item.get("average_price") or item.get("average_buy_price") or "0"
        entry_premium = Decimal(str(avg_price_raw)) / 100
        if entry_premium <= 0:
            logger.warning("Reconcile: skipping %s — no usable average_price", ticker)
            return None

        created_raw = item.get("created_at") or item.get("opened_at")
        try:
            opened_at = datetime.fromisoformat(created_raw) if created_raw else datetime.now(timezone.utc)
        except Exception:
            opened_at = datetime.now(timezone.utc)

        contract = OptionContract(
            ticker=ticker,
            expiry=expiry,
            strike=strike,
            type=option_type,
            bid=Decimal("0"),
            ask=Decimal("0"),
            mid=entry_premium,
            open_interest=0,
            volume=0,
        )

        position_id = instrument_id or f"reconciled_{ticker}_{expiry}_{strike}_{option_type}"

        return Position(
            position_id=position_id,
            ticker=ticker,
            contract=contract,
            entry_premium=entry_premium,
            target_level=None,
            opened_at=opened_at,
            quantity=quantity,
            option_instrument_id=instrument_id,
        )

    except Exception as exc:
        logger.warning("Reconcile: failed to parse position item: %s — %s", item, exc)
        return None


async def reconcile_positions(
    rh_tools: dict[str, "BaseTool"],
    position_store: "PositionStore",
    account_number: str,
) -> int:
    """
    Fetch open option positions from RH and re-populate PositionStore.
    Returns the number of positions recovered.
    """
    logger.info("Reconciling open positions from Robinhood…")
    try:
        result = await rh_call(rh_tools, "get_option_positions", {
            "account_number": account_number,
            "nonzero": True,
        })
    except Exception as exc:
        logger.error("Reconciliation failed — could not fetch option positions: %s", exc)
        return 0

    items = _parse_positions(result)
    recovered = 0
    for item in items:
        pos = _to_position(item)
        if pos is None:
            continue
        await position_store.add(pos)
        logger.warning(
            "Reconciled position: %s %s %s %.0f exp=%s qty=%d entry_premium=%s"
            " [profit_target disabled — stop-loss and DTE active]",
            pos.ticker, pos.contract.type, pos.contract.strike,
            float(pos.contract.strike), pos.contract.expiry,
            pos.quantity, pos.entry_premium,
        )
        recovered += 1

    if recovered:
        logger.warning(
            "Reconciled %d open position(s) from Robinhood. "
            "Profit targets are disabled for reconciled positions — "
            "stop-loss and DTE floor are active.",
            recovered,
        )
    else:
        logger.info("Reconciliation complete — no open positions found in Robinhood")

    return recovered


def _parse_orders(result: object) -> list[dict]:
    result = _unwrap_mcp(result)
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        inner = result.get("data", result)
        if isinstance(inner, dict):
            return inner.get("results", inner.get("orders", []))
        return result.get("results", result.get("orders", []))
    return []


def _order_to_position(order: dict) -> Position | None:
    """Convert one pending agentic order dict to a Position for monitoring.

    Returns None if required fields are missing or the order is not in a
    pending state that warrants tracking.
    """
    try:
        state: str = (order.get("state") or "").lower()
        # Only track orders that are still pending fill
        if state not in ("queued", "confirmed", "unconfirmed", "pending"):
            return None

        option_legs: list = order.get("legs", []) or []
        if not option_legs:
            return None
        leg = option_legs[0] if isinstance(option_legs[0], dict) else {}

        ticker: str = (
            order.get("chain_symbol")
            or order.get("symbol")
            or leg.get("chain_symbol")
            or leg.get("symbol")
            or ""
        ).upper()
        if not ticker:
            return None

        expiry_str: str = (
            leg.get("expiration_date")
            or order.get("expiration_date")
            or ""
        )
        if not expiry_str:
            return None
        expiry = date.fromisoformat(expiry_str)

        strike_raw = leg.get("strike_price") or order.get("strike_price") or "0"
        strike = Decimal(str(strike_raw))
        if not strike:
            return None

        option_type: str = (leg.get("option_type") or order.get("option_type") or "").lower()
        if option_type not in ("call", "put"):
            return None

        quantity_str = order.get("quantity") or order.get("contracts") or "1"
        quantity = int(Decimal(str(quantity_str)))
        if quantity <= 0:
            quantity = 1

        # Use order price as a proxy for entry premium
        price_raw = (
            order.get("price")
            or order.get("limit_price")
            or leg.get("price")
            or "0"
        )
        entry_premium = Decimal(str(price_raw))
        # If price is missing we still track with a zero premium; stop-loss
        # will be inactive but DTE protection remains.

        order_id: str = str(order.get("id") or order.get("order_id") or "")
        position_id = order_id or f"pending_{ticker}_{expiry}_{strike}_{option_type}"

        created_raw = order.get("created_at") or order.get("placed_at")
        try:
            opened_at = datetime.fromisoformat(created_raw) if created_raw else datetime.now(timezone.utc)
        except Exception:
            opened_at = datetime.now(timezone.utc)

        contract = OptionContract(
            ticker=ticker,
            expiry=expiry,
            strike=strike,
            type=option_type,
            bid=Decimal("0"),
            ask=Decimal("0"),
            mid=entry_premium,
            open_interest=0,
            volume=0,
        )

        return Position(
            position_id=position_id,
            ticker=ticker,
            contract=contract,
            entry_premium=entry_premium,
            target_level=None,
            opened_at=opened_at,
            quantity=quantity,
            option_instrument_id=order_id or None,
        )

    except Exception as exc:
        logger.warning("reconcile_open_orders: failed to parse order: %s — %s", order, exc)
        return None


async def reconcile_open_orders(
    rh_tools: dict[str, "BaseTool"],
    position_store: "PositionStore",
) -> int:
    """Fetch pending agentic option orders and add any that are not already
    tracked in PositionStore.

    This handles the case where a container restart occurs while an order is
    in flight (queued/confirmed but not yet filled). The exit loop will then
    monitor the position once it fills.

    Returns the number of orders added to the store.
    If the RH API does not support the filter parameters the function logs a
    warning and returns 0 without raising.
    """
    logger.info("Reconciling pending agentic option orders from Robinhood…")
    try:
        result = await rh_call(rh_tools, "get_option_orders", {
            "state": "queued",
            "placed_by_agent": True,
        })
    except Exception as exc:
        # Gracefully degrade — the API may not support these filter params
        logger.warning(
            "reconcile_open_orders: get_option_orders failed (%s) — skipping order reconciliation",
            exc,
        )
        return 0

    orders = _parse_orders(result)
    logger.info("reconcile_open_orders: found %d order(s) from API", len(orders))

    existing_ids: set[str] = {
        pos.position_id for pos in await position_store.all()
    }

    added = 0
    for order in orders:
        pos = _order_to_position(order)
        if pos is None:
            continue
        if pos.position_id in existing_ids:
            logger.debug(
                "reconcile_open_orders: order %s already in PositionStore — skipping",
                pos.position_id,
            )
            continue
        await position_store.add(pos)
        logger.warning(
            "reconcile_open_orders: added pending order %s %s %s %s exp=%s"
            " (state=%s) to PositionStore for monitoring",
            pos.ticker, pos.contract.type, pos.contract.strike, pos.position_id,
            pos.contract.expiry, order.get("state", "?"),
        )
        added += 1

    if added:
        logger.warning("reconcile_open_orders: added %d pending order(s) to PositionStore", added)
    else:
        logger.info("reconcile_open_orders: no new pending orders to track")

    return added
