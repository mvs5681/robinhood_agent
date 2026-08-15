"""Tests for FlowWatcher._poll()'s intraday flow-alert capture wiring —
see flow_capture.py for why this exists (get_flow_alerts has no historical
date= filtering, so this is the only way to recover real intraday flow
timing for backtest replay)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from trader.executor.schemas import ExecutionMode
from trader.live.cache import GEXCache
from trader.live.proposals import ProposalStore
from trader.live.watcher import FlowWatcher

_RAW_ALERT = {
    "ticker": "AAPL", "expiry": "2026-09-18", "strike": "200", "type": "call",
    "total_premium": "250000", "total_size": 500, "volume": 3000,
    "open_interest": 10000, "alert_rule": "RepeatedHits", "trade_count": 12,
    "underlying_price": "195", "created_at": "2026-08-15T14:30:00Z",
}


def _flow_tool(alerts: list[dict]) -> MagicMock:
    t = MagicMock()
    t.ainvoke = AsyncMock(return_value={"data": alerts})
    return t


def _watcher(flow_capture=None, uw_tools=None) -> FlowWatcher:
    return FlowWatcher(
        uw_tools=uw_tools or {"get_flow_alerts": _flow_tool([_RAW_ALERT])},
        cache=GEXCache(),
        proposal_store=ProposalStore(),
        execution_mode=ExecutionMode.PROPOSE_ONLY,
        executor=MagicMock(),
        flow_capture=flow_capture,
    )


class TestFlowCaptureWiring:
    async def test_poll_records_fetched_alerts(self):
        capture = MagicMock()
        watcher = _watcher(flow_capture=capture)

        await watcher._poll()

        capture.record.assert_called_once()
        recorded = capture.record.call_args[0][0]
        assert len(recorded) == 1
        assert recorded[0].ticker == "AAPL"

    async def test_poll_records_even_when_ticker_not_in_gex_cache(self):
        # Capture must see everything fetched, not just alerts for tickers
        # the scanner already has cached — that's the whole point (maximize
        # future backtest coverage, not just what today's pipeline can act on).
        capture = MagicMock()
        watcher = _watcher(flow_capture=capture)
        assert "AAPL" not in watcher.cache.tickers

        await watcher._poll()

        capture.record.assert_called_once()

    async def test_poll_works_with_no_flow_capture_configured(self):
        # Default None must not break polling — capture is optional.
        watcher = _watcher(flow_capture=None)
        await watcher._poll()  # no exception

    async def test_poll_does_not_call_capture_on_fetch_failure(self):
        failing_tool = MagicMock()
        failing_tool.ainvoke = AsyncMock(side_effect=RuntimeError("UW down"))
        capture = MagicMock()
        watcher = _watcher(flow_capture=capture, uw_tools={"get_flow_alerts": failing_tool})

        await watcher._poll()

        capture.record.assert_not_called()
