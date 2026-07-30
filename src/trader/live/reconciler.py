"""Startup position reconciliation.

On container restart, the in-memory PositionStore is empty even if open
option positions exist in Robinhood. This module re-populates the store
from get_option_positions so the exit loop can manage them immediately.

get_option_positions alone is not enough to build a Position: it has no
strike_price, and its own "type" field means long/short (not call/put).
Each position's option_id is batch-looked-up via get_option_instruments to
get the real strike/call-put type — without this, every position is
silently dropped (found live: reconciliation reported "no open positions"
while 3 were genuinely open, for however long this bug existed).

Limitations of reconciled positions:
- entry_premium: taken from RH average_price (cost basis / 100 per share)
- target_level:  None — profit target disabled; stop-loss and DTE still active
- opened_at:     taken from RH created_at when available, else now()
"""

from __future__ import annotations

import asyncio
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


def _to_position(item: dict, instrument: dict | None = None) -> Position | None:
    """Convert one RH position dict to a Position. Returns None if required fields missing.

    get_option_positions does not return strike_price or an option type —
    its own "type" field means "long"/"short" (position direction), not
    "call"/"put". Every real position dict is missing both fields the
    moment this function needs them, so it silently rejected every
    position it was ever given until reconcile_positions started passing
    the matching get_option_instruments record via `instrument`, which is
    where strike_price and the real call/put type actually live.
    """
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

        inst = instrument or {}
        strike = Decimal(str(inst.get("strike_price") or item.get("strike_price") or item.get("strike") or 0))
        if not strike:
            logger.debug("Reconcile: skipping %s — no strike (instrument lookup missing?)", ticker)
            return None

        # inst["type"] is the instrument's call/put; item.get("option_type")
        # is a legacy fallback for a differently-shaped position record —
        # item.get("type") is deliberately NOT used here, it's long/short.
        option_type: str = str(inst.get("type") or item.get("option_type") or "").lower()
        if option_type not in ("call", "put"):
            logger.debug("Reconcile: skipping %s — no call/put type (instrument lookup missing?)", ticker)
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


async def _fetch_positions_once(
    rh_tools: dict[str, "BaseTool"], account_number: str
) -> list[dict]:
    result = await rh_call(rh_tools, "get_option_positions", {
        "account_number": account_number,
        "nonzero": True,
    })
    return _parse_positions(result), result


def _parse_instruments(result: object) -> list[dict]:
    result = _unwrap_mcp(result)
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        inner = result.get("data", result)
        if isinstance(inner, dict):
            return inner.get("results", inner.get("instruments", []))
        return result.get("results", result.get("instruments", []))
    return []


async def _fetch_instrument_details(
    rh_tools: dict[str, "BaseTool"], option_ids: list[str]
) -> dict[str, dict]:
    """Batch-fetch strike_price/type (call/put) for a set of instrument ids —
    see _to_position for why get_option_positions alone isn't enough."""
    ids = [i for i in dict.fromkeys(option_ids) if i]  # dedup, preserve order
    if not ids:
        return {}
    try:
        result = await rh_call(rh_tools, "get_option_instruments", {"ids": ",".join(ids)})
    except Exception as exc:
        logger.error("reconcile_positions: instrument lookup failed: %s", exc)
        return {}
    items = _parse_instruments(result)
    return {i["id"]: i for i in items if isinstance(i, dict) and i.get("id")}


async def reconcile_positions(
    rh_tools: dict[str, "BaseTool"],
    position_store: "PositionStore",
    account_number: str,
) -> int:
    """
    Fetch open option positions from RH and re-populate PositionStore.
    Returns the number of positions recovered.

    On startup this is the sole source of truth for what's already open —
    a false "0 positions" here leaves real, already-paid-for contracts with
    no stop-loss/DTE/thesis protection for the rest of the session. A live
    incident showed reconcile getting an empty result while the account
    genuinely held 3 positions, with MCP session churn visible in the logs
    around the same moment — so a single empty result is retried once after
    a short delay before being accepted as ground truth.
    """
    logger.info("Reconciling open positions from Robinhood…")
    items: list[dict] = []
    raw_result: object = None
    for attempt in (1, 2):
        try:
            items, raw_result = await _fetch_positions_once(rh_tools, account_number)
        except Exception as exc:
            logger.error("Reconciliation failed — could not fetch option positions: %s", exc)
            return 0
        if items:
            break
        if attempt == 1:
            logger.warning("reconcile_positions: 0 items on first attempt — retrying once")
            await asyncio.sleep(3)

    if not items:
        # Zero positions is either genuinely correct or a silent parse/API
        # miss — log the raw shape so a real occurrence is diagnosable from
        # container logs alone instead of requiring a live re-query to prove
        # or disprove (this exact gap cost real diagnosis time in practice).
        logger.info("reconcile_positions: 0 items parsed after retry — raw response: %s",
                    str(raw_result)[:500])
    option_ids = [item.get("option_id") or item.get("id") for item in items]
    instruments = await _fetch_instrument_details(rh_tools, option_ids)

    recovered = 0
    for item in items:
        oid = item.get("option_id") or item.get("id")
        pos = _to_position(item, instruments.get(oid))
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
