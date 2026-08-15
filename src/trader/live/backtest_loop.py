"""Daily incremental backtest replay against data/history/ captures.

Builds a cumulative simulated track record — as opposed to a stateless
"what would the current strategy config have done over the whole window"
replay (that's what scripts/run_backtest.py / BacktestHarness.run() are
for) — so the dashboard can compare simulated performance against what the
live account actually did over the same period, to help surface gaps
between backtested and real strategy behavior.

Runs once per trading day, ~30 min after CaptureLoop/StateCaptureLoop finish
(5:00pm ET) so that day's fixtures are guaranteed to be on disk first.
Read-only against data/history/ and fully isolated from the live trading
loops — a failure here never touches RiskEngine/PositionStore/Executor.

The harness's async call chain does no genuine I/O yielding when running
against local JSON fixtures (BacktestDataSlice's tool shims are "async" in
signature only), so DataStore.load()'s synchronous file reads would block
this process's shared event loop — and with it FlowWatcher's 60s poll and
ExitLoop's 20s poll — for the full replay duration if called directly.
_run_harness_sync() is run via asyncio.to_thread() specifically to avoid
that: it gets its own event loop in a separate OS thread, fully isolated
from the live loops.

Persists two files:
  backtest_state.json    — full resumable ReplayState (open positions,
                            cash, complete trade log, equity curve).
                            Operational state, not served to the dashboard.
  backtest_results.json  — display payload derived from state, read by the
                            dashboard's /api/backtest endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    from datetime import timezone as _tz
    _ET = _tz(timedelta(hours=-4))  # type: ignore[assignment]

from trader.backtest.data_store import DataStore
from trader.backtest.harness import BacktestHarness
from trader.backtest.metrics import BacktestResult, PortfolioMetrics, TradeMetrics, compute_backtest_result
from trader.backtest.policy import StandardPolicy
from trader.backtest.state import ReplayState
from trader.live.state_capture import StateCapture

if TYPE_CHECKING:
    from trader.backtest.schemas import BacktestTradeRecord

logger = logging.getLogger(__name__)

_RUN_TIME = time(17, 0)      # 5:00pm ET — 30 min after the 4:30pm capture
_WINDOW_DAYS = 90            # trailing-window display slice (see class docstring)
_MIN_COVERAGE_DAYS = 3       # tickers need this many captured days before inclusion
_DEFAULT_CAPITAL = 2000.0    # matches real funding plan, not an arbitrary round number
_MAX_TRADES_IN_PAYLOAD = 200 # cap the trade table; metrics are still computed over all-time


def _seconds_until_next_run() -> float:
    """Return seconds until the next 5:00pm ET on a weekday."""
    now = datetime.now(_ET)
    target = now.replace(hour=_RUN_TIME.hour, minute=_RUN_TIME.minute, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return max((target - now).total_seconds(), 1.0)


def _run_harness_sync(harness: BacktestHarness, state: ReplayState) -> ReplayState:
    """Run step_forward() to completion on its own event loop in whatever
    thread this is called from — see module docstring for why this must
    never run directly on the shared live-loop event loop."""
    return asyncio.run(harness.step_forward(state))


# ---------------------------------------------------------------------------
# Display payload serialization
# ---------------------------------------------------------------------------


def _trade_metrics_dict(m: TradeMetrics) -> dict:
    return asdict(m)


def _portfolio_metrics_dict(m: PortfolioMetrics | None) -> dict | None:
    if m is None:
        return None
    d = asdict(m)
    d["equity_curve"] = [[d_.isoformat(), v] for d_, v in m.equity_curve]
    return d


def _result_dict(result: BacktestResult) -> dict:
    return {
        "overall": _trade_metrics_dict(result.overall),
        "by_regime": {k: _trade_metrics_dict(v) for k, v in result.by_regime.items()},
        "by_setup_type": {k: _trade_metrics_dict(v) for k, v in result.by_setup_type.items()},
        "by_regime_and_setup": {k: _trade_metrics_dict(v) for k, v in result.by_regime_and_setup.items()},
        "portfolio": _portfolio_metrics_dict(result.portfolio),
    }


def _trade_dict(r: "BacktestTradeRecord") -> dict:
    pos = r.position
    sig = r.exit_signal
    return {
        "entry_date": r.entry_date.isoformat(),
        "exit_date": r.exit_date.isoformat() if r.exit_date else None,
        "status": r.status,
        "ticker": pos.ticker,
        "option_type": pos.contract.type,
        "strike": float(pos.contract.strike),
        "expiry": pos.contract.expiry.isoformat(),
        "contracts": pos.contracts,
        "entry_premium": float(pos.entry_premium),
        "entry_cost": float(pos.entry_cost),
        "exit_premium": float(sig.current_premium) if sig else None,
        "exit_reason": sig.reason.value if sig else None,
        "pnl_pct": r.pnl_pct,
        "pnl_dollars": r.pnl_dollars,
        "regime": r.candidate.gex_setup.regime.value,
        "setup_type": r.candidate.gex_setup.setup_type,
        "composite_score": r.candidate.blend_scores.composite,
    }


def _build_display_payload(
    state: ReplayState,
    *,
    start_date: date,
    end_date: date,
    tickers: list[str],
    window_days: int,
    initial_capital: float,
    bypass_flow_gate: bool,
) -> dict:
    all_time = compute_backtest_result(
        state.records, equity_curve=state.equity_curve, initial_capital=initial_capital,
    )

    # Trailing-window slice is %-only (win rate, avg P&L, regime/setup
    # breakdowns) — no portfolio $ metrics here, since "starting capital"
    # for a mid-simulation slice isn't $initial_capital and computing
    # total_return_pct/drawdown against that base would be misleading.
    cutoff = end_date - timedelta(days=window_days)
    windowed_records = [r for r in state.records if r.entry_date >= cutoff]
    windowed = compute_backtest_result(windowed_records)

    recent_trades = sorted(state.records, key=lambda r: r.entry_date, reverse=True)
    recent_trades = recent_trades[:_MAX_TRADES_IN_PAYLOAD]

    return {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "window_days": window_days,
        "tickers": tickers,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "trading_days": len(state.processed_dates),
        "initial_capital": initial_capital,
        "bypass_flow_gate": bypass_flow_gate,
        "all_time": _result_dict(all_time),
        "trailing_window": _result_dict(windowed),
        "trades": [_trade_dict(r) for r in recent_trades],
    }


# ---------------------------------------------------------------------------
# BacktestLoop
# ---------------------------------------------------------------------------


class BacktestLoop:
    """Background coroutine: nightly incremental backtest replay against
    data/history/, producing a cumulative track record for the dashboard."""

    def __init__(
        self,
        history_dir: str | Path = "data/history",
        state_file: str | Path = "data/backtest_state.json",
        results_file: str | Path = "data/backtest_results.json",
        initial_capital: float = _DEFAULT_CAPITAL,
        max_concurrent_positions: int = 3,
        window_days: int = _WINDOW_DAYS,
        min_coverage_days: int = _MIN_COVERAGE_DAYS,
        bypass_flow_gate: bool = True,
    ) -> None:
        # Defaults to True: verified live that flow_alerts.json captures are
        # a single end-of-day snapshot of "whatever's currently in the UW
        # feed" — the alerts in a given day's capture can be many hours
        # stale relative to that day's replay clock (confirmed: a 2026-08-14
        # capture held alerts timestamped 2026-08-12), which is always
        # outside FlowTrigger's default 4h lookback. Without bypassing,
        # every candidate is rejected at the flow gate, every day, making
        # the whole comparison feature produce zero simulated trades against
        # real captured data. Bypassing tests GEX regime + selector + exit
        # logic only — not the flow-confirmation edge — same tradeoff
        # scripts/fetch_polygon_history.py's backtests already accept.
        # Revisit once intraday flow-alert logging (TODO.md) lands.
        self._history_dir = Path(history_dir)
        self._state_file = Path(state_file)
        self._results_file = Path(results_file)
        self._initial_capital = initial_capital
        self._max_concurrent_positions = max_concurrent_positions
        self._window_days = window_days
        self._min_coverage_days = min_coverage_days
        self._bypass_flow_gate = bypass_flow_gate

    async def run(self) -> None:
        logger.info(
            "BacktestLoop started — capital=$%.0f window=%dd min_coverage=%dd bypass_flow_gate=%s",
            self._initial_capital, self._window_days, self._min_coverage_days, self._bypass_flow_gate,
        )
        while True:
            secs = _seconds_until_next_run()
            logger.debug("BacktestLoop: sleeping %.0f s until next run", secs)
            await asyncio.sleep(secs)
            try:
                await self.run_once()
            except Exception as exc:
                logger.error("BacktestLoop: run failed: %s", exc)

    async def run_once(self) -> ReplayState | None:
        """Step the replay forward through any newly-available dates and
        persist state + display payload. Returns the updated state, or None
        if there's no captured history yet. Public (not just internal to
        run()'s loop) so it can be triggered manually / tested directly."""
        t0 = _time.monotonic()
        store = DataStore(self._history_dir)
        available = store.available_dates()
        if not available:
            logger.info("BacktestLoop: no captured history yet — skipping")
            return None

        capture = StateCapture(self._history_dir)
        tickers = sorted(capture.covered_tickers(min_days=self._min_coverage_days).keys())
        if not tickers:
            logger.info(
                "BacktestLoop: no tickers with >= %d captured day(s) yet — skipping",
                self._min_coverage_days,
            )
            return None

        state = await asyncio.to_thread(self._load_state)
        already_processed = set(state.processed_dates)

        start_date = available[0]
        end_date = date.today()
        harness = BacktestHarness(
            policy=StandardPolicy(bypass_flow_gate=self._bypass_flow_gate),
            data_store=store,
            start_date=start_date,
            end_date=end_date,
            tickers=tickers,
            max_concurrent_positions=self._max_concurrent_positions,
            initial_capital=self._initial_capital,
        )

        state = await asyncio.to_thread(_run_harness_sync, harness, state)
        new_dates = [d for d in state.processed_dates if d not in already_processed]

        await asyncio.to_thread(self._persist_state, state)

        payload = _build_display_payload(
            state, start_date=start_date, end_date=end_date, tickers=tickers,
            window_days=self._window_days, initial_capital=self._initial_capital,
            bypass_flow_gate=self._bypass_flow_gate,
        )
        await asyncio.to_thread(self._persist_results, payload)

        ms = round((_time.monotonic() - t0) * 1000, 1)
        logger.info(
            "BacktestLoop: stepped %d new day(s) %s in %.0fms — "
            "%d cumulative trades across %d tickers",
            len(new_dates), new_dates, ms, len(state.records), len(tickers),
        )
        return state

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> ReplayState:
        if not self._state_file.exists():
            return ReplayState(cash=Decimal(str(self._initial_capital)))
        try:
            data = json.loads(self._state_file.read_text())
            return ReplayState.from_dict(data)
        except Exception as exc:
            logger.error(
                "BacktestLoop: could not load state from %s: %s — starting fresh",
                self._state_file, exc,
            )
            return ReplayState(cash=Decimal(str(self._initial_capital)))

    def _persist_state(self, state: ReplayState) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps(state.to_dict(), indent=2))

    def _persist_results(self, payload: dict) -> None:
        self._results_file.parent.mkdir(parents=True, exist_ok=True)
        self._results_file.write_text(json.dumps(payload, indent=2))
