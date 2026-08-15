"""Integration tests for BacktestLoop — the nightly incremental replay that
builds the dashboard's cumulative simulated track record.

Runs entirely on local fixtures (tests/fixtures/history/) — no live API
calls, no real event loop blocking risk since asyncio.to_thread is exercised
directly against the small fixture set.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from trader.live.backtest_loop import BacktestLoop

HISTORY_ROOT = Path(__file__).parent.parent / "fixtures" / "history"


def _loop(tmp_path: Path, **kwargs) -> BacktestLoop:
    return BacktestLoop(
        history_dir=HISTORY_ROOT,
        state_file=tmp_path / "backtest_state.json",
        results_file=tmp_path / "backtest_results.json",
        min_coverage_days=kwargs.pop("min_coverage_days", 1),
        **kwargs,
    )


def _stale_flow_history(tmp_path: Path) -> Path:
    """Copy the real fixture history but backdate flow_alerts.json's
    created_at well outside FlowTrigger's default 4h lookback — reproduces
    the real captured-data shape (a single stale end-of-day snapshot) that
    motivated bypass_flow_gate defaulting to True."""
    dest = tmp_path / "history"
    shutil.copytree(HISTORY_ROOT, dest)
    for alerts_file in dest.glob("*/flow_alerts.json"):
        data = json.loads(alerts_file.read_text())
        for alert in data.get("data", []):
            alert["created_at"] = "2025-12-25T14:30:00Z"  # days stale, any day
        alerts_file.write_text(json.dumps(data))
    return dest


class TestRunOnce:
    async def test_processes_both_fixture_dates(self, tmp_path):
        loop = _loop(tmp_path)
        state = await loop.run_once()

        assert state is not None
        assert len(state.processed_dates) == 2

    async def test_persists_state_file(self, tmp_path):
        loop = _loop(tmp_path)
        await loop.run_once()

        state_file = tmp_path / "backtest_state.json"
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert "records" in data
        assert len(data["records"]) >= 1

    async def test_persists_results_file_with_expected_shape(self, tmp_path):
        loop = _loop(tmp_path)
        await loop.run_once()

        results = json.loads((tmp_path / "backtest_results.json").read_text())
        assert results["tickers"] == ["AAPL"]
        assert results["trading_days"] == 2
        assert "all_time" in results and "trailing_window" in results
        assert results["all_time"]["overall"]["trade_count"] >= 1
        assert isinstance(results["trades"], list)
        assert results["trades"][0]["ticker"] == "AAPL"

    async def test_all_time_includes_profit_target_trade(self, tmp_path):
        loop = _loop(tmp_path)
        await loop.run_once()

        results = json.loads((tmp_path / "backtest_results.json").read_text())
        reasons = {t["exit_reason"] for t in results["trades"] if t["exit_reason"]}
        assert "profit_target" in reasons

    async def test_second_call_is_idempotent_no_new_dates(self, tmp_path):
        loop = _loop(tmp_path)
        state1 = await loop.run_once()
        state2 = await loop.run_once()

        assert state2.processed_dates == state1.processed_dates
        assert len(state2.records) == len(state1.records)

    async def test_no_history_dir_returns_none_and_writes_nothing(self, tmp_path):
        loop = BacktestLoop(
            history_dir=tmp_path / "does_not_exist",
            state_file=tmp_path / "state.json",
            results_file=tmp_path / "results.json",
        )
        result = await loop.run_once()

        assert result is None
        assert not (tmp_path / "state.json").exists()
        assert not (tmp_path / "results.json").exists()

    async def test_coverage_threshold_excludes_thin_tickers(self, tmp_path):
        # AAPL only has 2 days of coverage in the fixture manifest —
        # requiring 5 must exclude it entirely, same as "no tickers yet"
        loop = _loop(tmp_path, min_coverage_days=5)
        result = await loop.run_once()
        assert result is None

    async def test_trailing_window_has_no_portfolio_dollar_metrics(self, tmp_path):
        # A mid-simulation slice doesn't have a meaningful "starting capital"
        # of its own — see backtest_loop.py's _build_display_payload comment.
        # (The fixture's January 2026 trades are far outside any reasonable
        # trailing window measured from the real "today", so trade_count
        # here is expectedly 0 — see the include/exclude tests below for
        # window-boundary behavior specifically.)
        loop = _loop(tmp_path, window_days=90)
        await loop.run_once()

        results = json.loads((tmp_path / "backtest_results.json").read_text())
        assert results["all_time"]["portfolio"] is not None
        assert results["trailing_window"]["portfolio"] is None

    async def test_trailing_window_excludes_trades_outside_it(self, tmp_path):
        # A 0-day window excludes everything entered before "today" — the
        # fixture trades are all from January 2026, long before "today".
        loop = _loop(tmp_path, window_days=0)
        await loop.run_once()

        results = json.loads((tmp_path / "backtest_results.json").read_text())
        assert results["trailing_window"]["overall"]["trade_count"] == 0
        assert results["all_time"]["overall"]["trade_count"] >= 1

    async def test_trailing_window_includes_trades_inside_it(self, tmp_path):
        # A window wide enough to reach back to the fixture's January 2026
        # dates from the real "today" must include those trades — proves
        # the cutoff filter's boundary condition, not just its exclusion.
        loop = _loop(tmp_path, window_days=365 * 100)
        await loop.run_once()

        results = json.loads((tmp_path / "backtest_results.json").read_text())
        assert (results["trailing_window"]["overall"]["trade_count"]
                == results["all_time"]["overall"]["trade_count"])
        assert results["all_time"]["overall"]["trade_count"] >= 1

    async def test_display_capital_matches_configured_default(self, tmp_path):
        loop = _loop(tmp_path, initial_capital=2000.0)
        await loop.run_once()
        results = json.loads((tmp_path / "backtest_results.json").read_text())
        assert results["initial_capital"] == 2000.0
        assert results["all_time"]["portfolio"]["initial_capital"] == 2000.0

    async def test_resuming_with_persisted_state_does_not_duplicate_trades(self, tmp_path):
        # First loop instance processes and persists; a brand new BacktestLoop
        # pointed at the same files must resume from disk, not start fresh.
        loop1 = _loop(tmp_path)
        state1 = await loop1.run_once()

        loop2 = _loop(tmp_path)
        state2 = await loop2.run_once()

        assert state2.processed_dates == state1.processed_dates
        assert len(state2.records) == len(state1.records)


class TestBypassFlowGate:
    async def test_defaults_to_true_and_is_surfaced_in_results(self, tmp_path):
        loop = _loop(tmp_path)
        await loop.run_once()
        results = json.loads((tmp_path / "backtest_results.json").read_text())
        assert results["bypass_flow_gate"] is True

    async def test_explicit_false_is_surfaced_in_results(self, tmp_path):
        loop = _loop(tmp_path, bypass_flow_gate=False)
        await loop.run_once()
        results = json.loads((tmp_path / "backtest_results.json").read_text())
        assert results["bypass_flow_gate"] is False

    async def test_stale_flow_alerts_block_entry_when_not_bypassed(self, tmp_path):
        # Reproduces the real captured-data shape: flow_alerts.json is a
        # single end-of-day snapshot that can be well outside FlowTrigger's
        # lookback window. Without bypass, this must reject every entry —
        # exactly the bug that motivated bypass_flow_gate defaulting to True.
        stale_history = _stale_flow_history(tmp_path)
        loop = BacktestLoop(
            history_dir=stale_history,
            state_file=tmp_path / "state.json",
            results_file=tmp_path / "results.json",
            min_coverage_days=1,
            bypass_flow_gate=False,
        )
        state = await loop.run_once()
        assert len(state.records) == 0

    async def test_same_stale_flow_alerts_allow_entry_when_bypassed(self, tmp_path):
        stale_history = _stale_flow_history(tmp_path)
        loop = BacktestLoop(
            history_dir=stale_history,
            state_file=tmp_path / "state.json",
            results_file=tmp_path / "results.json",
            min_coverage_days=1,
            bypass_flow_gate=True,
        )
        state = await loop.run_once()
        assert len(state.records) >= 1
