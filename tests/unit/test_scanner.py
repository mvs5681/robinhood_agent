"""Tests for GEXScanner ticker-discovery merge logic.

Focused on _held_tickers()/_discover_tickers()'s handling of open positions —
the fix that keeps a held ticker's GEXCache entry fresh (so thesis-invalidation
and trailing-stop checks in ExitLoop don't silently go stale) even after the
ticker stops trending and would otherwise fall out of discovery.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from trader.exits.schemas import Position
from trader.live.cache import GEXCache
from trader.live.position_store import PositionStore
from trader.live.scanner import GEXScanner
from trader.uw.schemas import OptionContract


def _position(ticker: str) -> Position:
    return Position(
        position_id=f"pos-{ticker}", ticker=ticker,
        contract=OptionContract(
            ticker=ticker, expiry=date(2026, 8, 7), strike=Decimal("100"),
            type="call", bid=Decimal("1.00"), ask=Decimal("1.10"),
            open_interest=100, volume=50,
        ),
        entry_premium=Decimal("1.05"), target_level=None,
        opened_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )


def _flow_alerts_tool(empty: bool = True) -> MagicMock:
    t = MagicMock()
    # empty response on both issue-type slices — discovery finds nothing new
    t.ainvoke = AsyncMock(return_value={"data": []} if empty else {})
    return t


def _scanner(position_store: PositionStore | None = None, seed_tickers=None) -> GEXScanner:
    return GEXScanner(
        uw_tools={"get_flow_alerts": _flow_alerts_tool()},
        cache=GEXCache(),
        seed_tickers=seed_tickers,
        position_store=position_store,
    )


class TestHeldTickers:
    async def test_empty_without_position_store(self):
        scanner = _scanner(position_store=None)
        assert await scanner._held_tickers() == []

    async def test_empty_with_no_open_positions(self):
        scanner = _scanner(position_store=PositionStore())
        assert await scanner._held_tickers() == []

    async def test_returns_unique_held_tickers(self):
        store = PositionStore()
        await store.add(_position("CRWV"))
        await store.add(_position("IWM"))
        scanner = _scanner(position_store=store)
        held = await scanner._held_tickers()
        assert set(held) == {"CRWV", "IWM"}

    async def test_lookup_failure_degrades_to_empty(self):
        store = MagicMock()
        store.all = AsyncMock(side_effect=RuntimeError("boom"))
        scanner = _scanner(position_store=store)
        assert await scanner._held_tickers() == []


class TestDiscoverTickersIncludesHeldPositions:
    async def test_held_ticker_included_even_with_no_discovery_activity(self):
        store = PositionStore()
        await store.add(_position("CRWV"))
        scanner = _scanner(position_store=store)
        tickers, _ = await scanner._discover_tickers()
        assert "CRWV" in tickers

    async def test_held_ticker_merged_with_seed_and_deduped(self):
        store = PositionStore()
        await store.add(_position("CRWV"))
        await store.add(_position("SPY"))  # also a seed ticker — must not duplicate
        scanner = _scanner(position_store=store, seed_tickers=["SPY", "QQQ"])
        tickers, _ = await scanner._discover_tickers()
        assert tickers.count("SPY") == 1
        assert set(tickers) == {"SPY", "QQQ", "CRWV"}

    async def test_held_ticker_survives_total_discovery_failure(self):
        store = PositionStore()
        await store.add(_position("CRWV"))
        failing_tool = MagicMock()
        failing_tool.ainvoke = AsyncMock(side_effect=RuntimeError("UW down"))
        scanner = GEXScanner(
            uw_tools={"get_flow_alerts": failing_tool},
            cache=GEXCache(),
            seed_tickers=["SPY"],
            position_store=store,
        )
        tickers, _ = await scanner._discover_tickers()
        assert set(tickers) == {"SPY", "CRWV"}
