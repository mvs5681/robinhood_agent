"""Unit tests for CaptureLoop's held-position contract coverage.

get_options_chain returns an arbitrary ~50 contracts (see
docs/ARCHITECTURE.md §2), not a guaranteed DTE window — a held position can
age past whatever window the daily capture happens to return, silently
dropping its exact contract from that day's option_contracts.json. That
breaks should_exit() in backtest replay (get_option_premium returns None,
short-circuiting every exit check, including thesis invalidation) even
though the wiring for THESIS_INVALIDATED itself is correct. These tests
cover the get_options_screener augmentation added to close that gap.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

from trader.exits.schemas import Position
from trader.live.capture_loop import capture_day
from trader.live.position_store import PositionStore
from trader.uw.schemas import OptionContract


def _contract(ticker="IBIT", strike="38", expiry=date(2026, 8, 14), type_="call") -> OptionContract:
    return OptionContract(
        ticker=ticker, expiry=expiry, strike=Decimal(strike), type=type_,
        bid=Decimal("0.77"), ask=Decimal("0.78"), open_interest=1039, volume=256,
    )


def _position(contract: OptionContract) -> Position:
    return Position(
        position_id=f"pos_{contract.ticker}",
        ticker=contract.ticker,
        contract=contract,
        entry_premium=Decimal("0.775"),
        target_level=Decimal("40"),
        opened_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
    )


class _FakeTool:
    def __init__(self, name: str, responder) -> None:
        self.name = name
        self._responder = responder
        self.calls: list[dict] = []

    async def ainvoke(self, kwargs: dict) -> dict:
        self.calls.append(kwargs)
        return self._responder(kwargs)


def _base_tools(chain_data: list[dict], screener_data: list[dict] | None = None) -> dict[str, _FakeTool]:
    tools = {
        "get_market_tide": _FakeTool("get_market_tide", lambda kw: {"data": []}),
        "get_flow_alerts": _FakeTool("get_flow_alerts", lambda kw: {"data": []}),
        "get_greek_exposure_by_strike": _FakeTool("get_greek_exposure_by_strike", lambda kw: {"data": []}),
        "get_dark_pool_trades": _FakeTool("get_dark_pool_trades", lambda kw: {"data": []}),
        "get_flow_per_strike": _FakeTool("get_flow_per_strike", lambda kw: {"data": []}),
        "get_options_chain": _FakeTool("get_options_chain", lambda kw: {"data": chain_data}),
        "get_extended_technical_indicator": _FakeTool("get_extended_technical_indicator", lambda kw: {"data": []}),
    }
    if screener_data is not None:
        tools["get_options_screener"] = _FakeTool(
            "get_options_screener", lambda kw: {"data": screener_data}
        )
    return tools


class TestHeldPositionCoverage:
    async def test_held_ticker_captured_even_when_not_seed_or_discovered(self, tmp_path):
        store = PositionStore()
        await store.add(_position(_contract()))
        tools = _base_tools(chain_data=[])

        await capture_day(
            tools, date(2026, 7, 31), tmp_path, seeds=[], position_store=store,
        )

        assert (tmp_path / "2026-07-31" / "IBIT_option_contracts.json").exists()

    async def test_missing_contract_augmented_from_screener(self, tmp_path):
        held = _contract()
        store = PositionStore()
        await store.add(_position(held))
        # Base chain only has an unrelated contract — the held strike/expiry is absent.
        base_chain = [{
            "ticker": "IBIT", "expiry": "2026-08-21", "strike": "37", "type": "call",
            "bid": "0.63", "ask": "0.65", "open_interest": 100, "volume": 50,
        }]
        screener_hit = [{
            "ticker": "IBIT", "expiry": "2026-08-14", "strike": "38", "type": "call",
            "bid": "0.10", "ask": "0.12", "open_interest": 500, "volume": 30,
        }]
        tools = _base_tools(chain_data=base_chain, screener_data=screener_hit)

        await capture_day(
            tools, date(2026, 7, 31), tmp_path, seeds=[], position_store=store,
        )

        written = json.loads(
            (tmp_path / "2026-07-31" / "IBIT_option_contracts.json").read_text()
        )
        strikes = {(row["strike"], row["expiry"]) for row in written["data"]}
        assert ("38", "2026-08-14") in strikes
        assert tools["get_options_screener"].calls, "screener should have been called to fill the gap"
        call_kwargs = tools["get_options_screener"].calls[0]
        assert call_kwargs["type"] == "Calls"

    async def test_no_screener_call_when_contract_already_in_base_chain(self, tmp_path):
        held = _contract()
        store = PositionStore()
        await store.add(_position(held))
        base_chain = [{
            "ticker": "IBIT", "expiry": "2026-08-14", "strike": "38", "type": "call",
            "bid": "0.77", "ask": "0.78", "open_interest": 1039, "volume": 256,
        }]
        tools = _base_tools(chain_data=base_chain, screener_data=[])

        await capture_day(
            tools, date(2026, 7, 31), tmp_path, seeds=[], position_store=store,
        )

        assert not tools["get_options_screener"].calls

    async def test_expired_held_contract_skipped_not_fetched(self, tmp_path):
        # dte < 0 relative to trade_date — nothing to fetch, must not error.
        held = _contract(expiry=date(2026, 7, 1))
        store = PositionStore()
        await store.add(_position(held))
        tools = _base_tools(chain_data=[], screener_data=[])

        await capture_day(
            tools, date(2026, 7, 31), tmp_path, seeds=[], position_store=store,
        )

        assert not tools["get_options_screener"].calls

    async def test_no_position_store_behaves_as_before(self, tmp_path):
        tools = _base_tools(chain_data=[{"ticker": "AAPL", "expiry": "2026-08-21",
                                          "strike": "200", "type": "call",
                                          "bid": "1", "ask": "1.1",
                                          "open_interest": 10, "volume": 5}])

        await capture_day(
            tools, date(2026, 7, 31), tmp_path, seeds=["AAPL"], position_store=None,
        )

        assert (tmp_path / "2026-07-31" / "AAPL_option_contracts.json").exists()
