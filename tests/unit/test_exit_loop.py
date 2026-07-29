"""Tests for ExitLoop's thesis-invalidation wiring (GEXCache → ExitMonitor).

Price/DTE exit behavior itself is covered by test_exit_monitor.py; this file
covers the plumbing that makes the live GEX setup reach the monitor.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from trader.exits.monitor import ExitMonitor
from trader.exits.schemas import ExitReason, Position
from trader.executor.schemas import ExecutionMode
from trader.gex.schemas import GEXRegime, GEXSetup
from trader.live.cache import GEXCache, TickerSnapshot
from trader.live.exit_loop import ExitLoop
from trader.live.position_store import PositionStore
from trader.uw.schemas import OptionContract

ACCOUNT = "869536151"


def _contract() -> OptionContract:
    return OptionContract(
        ticker="AAPL", expiry=date(2026, 8, 14), strike=Decimal("200"),
        type="call", bid=Decimal("2.90"), ask=Decimal("3.10"),
        open_interest=9000, volume=4500,
    )


def _position() -> Position:
    return Position(
        position_id="pos-001", ticker="AAPL", contract=_contract(),
        entry_premium=Decimal("3.00"), target_level=Decimal("250"),  # far from spot
        opened_at=datetime(2026, 7, 1, tzinfo=timezone.utc), quantity=1,
        option_instrument_id="opt-1",
    )


def _setup(direction: str = "call", regime: GEXRegime = GEXRegime.NEGATIVE, stale: bool = False):
    return GEXSetup(
        ticker="AAPL", as_of=datetime.now(timezone.utc), spot_price=Decimal("195"),
        regime=regime, flip_point=None, nearest_call_wall=None, nearest_put_wall=None,
        target_level=Decimal("250"), candidate_direction=direction,
        setup_type="momentum" if direction != "none" else "none",
        structure_confidence=0.6, raw_gex_by_strike=[],
    )


async def _cache_with(ticker: str, setup, *, stale: bool = False) -> GEXCache:
    cache = GEXCache()
    snap = TickerSnapshot(gex_setup=setup)
    if not stale:
        snap.refreshed_at = datetime.now(timezone.utc)
    await cache.update([], {ticker: snap})
    return cache


def _quote_tool(mark: str = "3.00") -> MagicMock:
    t = MagicMock()
    t.ainvoke = AsyncMock(return_value={"data": {"results": [
        {"quote": {"mark_price": mark}}
    ]}})
    return t


def _loop(cache: GEXCache | None, rh_tools: dict | None = None,
          store: PositionStore | None = None) -> ExitLoop:
    return ExitLoop(
        rh_tools=rh_tools if rh_tools is not None else {"get_option_quotes": _quote_tool()},
        position_store=store or PositionStore(),
        account_number=ACCOUNT,
        execution_mode=ExecutionMode.PROPOSE_ONLY,
        monitor=ExitMonitor(stop_loss_pct=0.35, dte_floor=7),
        cache=cache,
    )


class TestCurrentGexSetup:
    async def test_returns_none_when_no_cache_wired(self):
        loop = _loop(cache=None)
        assert await loop._current_gex_setup("AAPL") is None

    async def test_returns_none_when_ticker_not_cached(self):
        cache = GEXCache()
        loop = _loop(cache=cache)
        assert await loop._current_gex_setup("AAPL") is None

    async def test_returns_live_setup_when_fresh(self):
        cache = await _cache_with("AAPL", _setup(direction="put"))
        loop = _loop(cache=cache)
        setup = await loop._current_gex_setup("AAPL")
        assert setup is not None
        assert setup.candidate_direction == "put"

    async def test_returns_none_when_stale(self):
        cache = await _cache_with("AAPL", _setup(direction="put"), stale=True)
        loop = _loop(cache=cache)
        assert await loop._current_gex_setup("AAPL") is None


class TestEvaluateWithThesisInvalidation:
    async def test_exit_triggered_when_live_direction_flips(self):
        store = PositionStore()
        pos = _position()
        await store.add(pos)
        cache = await _cache_with("AAPL", _setup(direction="put"))
        loop = _loop(cache=cache, store=store)

        # PROPOSE_ONLY dry-run — no RH calls needed, just confirm the signal fires
        await loop._evaluate(pos, {"AAPL": Decimal("195")})

        # dry-run exit still removes the position from the store
        assert await store.all() == []

    async def test_no_exit_when_cache_absent_and_price_dte_clear(self):
        store = PositionStore()
        pos = _position()
        await store.add(pos)
        loop = _loop(cache=None, store=store)
        await loop._evaluate(pos, {"AAPL": Decimal("195")})
        # nothing triggers: price far from target, premium unchanged, dte n/a
        # (option_mid_and_dte with no rh_tools returns dte only, mid=None → returns early)
        assert len(await store.all()) == 1
