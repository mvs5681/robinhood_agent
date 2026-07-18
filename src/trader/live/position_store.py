"""In-memory store for open positions opened by the live trading loop."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from trader.exits.schemas import Position

if TYPE_CHECKING:
    from trader.scoring.schemas import CandidateSignal
    from trader.executor.schemas import OrderResult


def make_position(
    candidate: "CandidateSignal",
    result: "OrderResult",
    quantity: int,
    instrument_id: str | None = None,
) -> Position | None:
    """Build a Position from a confirmed buy result. Returns None if data is missing."""
    sc = candidate.selected_contract
    gs = candidate.gex_setup
    if sc is None or gs is None:
        return None
    order_id = result.order_id or f"pos_{candidate.ticker}_{datetime.now(timezone.utc).timestamp():.0f}"
    entry = result.request.limit_price if result.request.limit_price is not None else sc.mid
    return Position(
        position_id=order_id,
        ticker=candidate.ticker,
        contract=sc,
        entry_premium=entry,
        target_level=gs.target_level,
        opened_at=datetime.now(timezone.utc),
        quantity=quantity,
        option_instrument_id=instrument_id,
    )


class PositionStore:
    """Thread-safe in-memory store for open positions."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._positions: dict[str, Position] = {}

    async def add(self, position: Position) -> None:
        async with self._lock:
            self._positions[position.position_id] = position

    async def remove(self, position_id: str) -> None:
        async with self._lock:
            self._positions.pop(position_id, None)

    async def all(self) -> list[Position]:
        async with self._lock:
            return list(self._positions.values())

    async def get(self, position_id: str) -> Position | None:
        async with self._lock:
            return self._positions.get(position_id)

    @property
    def count(self) -> int:
        return len(self._positions)
