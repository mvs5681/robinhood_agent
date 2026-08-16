"""Phase 8 — Policy adapters for the backtest harness.

PolicyAdapter is the ABC both live and backtest runners satisfy.
StandardPolicy wraps the full Phases 1-6 pipeline and delegates exits
to ExitMonitor.

The "live vs backtest" distinction is entirely in what tools are injected:
  - Backtest: BacktestDataSlice.as_tools() → mock tools returning historical data
  - Live:     real UW MCP tools

Swap is a one-line change: pass different tools to generate_and_score.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, time, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from trader.contracts.selector import SelectorParams
from trader.executor.schemas import ExecutionMode
from trader.exits.monitor import ExitMonitor
from trader.exits.schemas import ExitContext, ExitSignal
from trader.gex.detector import GEXDetector
from trader.gex.schemas import GEXDetectorParams, GEXSetup
from trader.graph.agent import run_pipeline
from trader.risk.schemas import RiskParams
from trader.scoring.schemas import CandidateSignal
from trader.uw.schemas import SpotGEXByStrike
from trader.uw.validators import (
    parse_interpolated_iv,
    parse_spot_gex_by_strike,
    parse_technical_indicator,
)

from .data_store import BacktestDataSlice
from .schemas import BacktestPosition

if TYPE_CHECKING:
    pass


class PolicyAdapter(ABC):
    """Common interface satisfied by live and backtest execution paths."""

    @abstractmethod
    async def generate_and_score(
        self,
        tickers: list[str],
        data_slice: BacktestDataSlice,
    ) -> list[CandidateSignal]:
        """Run Phases 1-6 and return all candidates (proposed or skipped)."""

    @abstractmethod
    def should_enter(self, candidate: CandidateSignal) -> bool:
        """True if this candidate should open a new position."""

    @abstractmethod
    def should_exit(
        self,
        position: BacktestPosition,
        data_slice: BacktestDataSlice,
    ) -> ExitSignal | None:
        """Evaluate whether an open position should be closed on this day."""


class StandardPolicy(PolicyAdapter):
    """
    Full Phases 1-6 pipeline as a PolicyAdapter.

    generate_and_score runs run_pipeline with the slice's mock tools,
    threading pipeline_date so the flow trigger uses the historical date.
    should_exit delegates to ExitMonitor using spot price and option premium
    resolved from the data slice.
    """

    def __init__(
        self,
        detector_params: GEXDetectorParams | None = None,
        blend_weights: dict[str, float] | None = None,
        flow_min_premium: Decimal = Decimal("100_000"),
        flow_lookback_hours: int = 4,
        selector_params: SelectorParams | None = None,
        risk_params: RiskParams | None = None,
        sector_map: dict[str, str] | None = None,
        exit_monitor: ExitMonitor | None = None,
        min_composite_score: float = 0.0,
        bypass_flow_gate: bool = False,
    ) -> None:
        self._detector_params = detector_params
        self._blend_weights = blend_weights
        self._flow_min_premium = flow_min_premium
        self._flow_lookback_hours = flow_lookback_hours
        self._selector_params = selector_params
        self._risk_params = risk_params
        self._sector_map = sector_map
        self._exit_monitor = exit_monitor or ExitMonitor()
        self._min_composite = min_composite_score
        self._bypass_flow_gate = bypass_flow_gate
        self._detector = GEXDetector(detector_params)

    async def generate_and_score(
        self,
        tickers: list[str],
        data_slice: BacktestDataSlice,
    ) -> list[CandidateSignal]:
        state = await run_pipeline(
            tickers=tickers,
            tools=data_slice.as_tools(),
            detector_params=self._detector_params,
            blend_weights=self._blend_weights,
            flow_min_premium=self._flow_min_premium,
            flow_lookback_hours=self._flow_lookback_hours,
            selector_params=self._selector_params,
            risk_params=self._risk_params,
            sector_map=self._sector_map,
            execution_mode=ExecutionMode.PROPOSE_ONLY,
            pipeline_date=data_slice.date,
            bypass_flow_gate=self._bypass_flow_gate,
        )
        return state.candidates

    def should_enter(self, candidate: CandidateSignal) -> bool:
        return (
            candidate.execution_status == "proposed"
            and candidate.selected_contract is not None
            and candidate.blend_scores.composite >= self._min_composite
        )

    def should_exit(
        self,
        position: BacktestPosition,
        data_slice: BacktestDataSlice,
    ) -> ExitSignal | None:
        current_price = data_slice.get_spot_price(position.ticker)
        current_premium = data_slice.get_option_premium(position.contract)
        if current_price is None or current_premium is None:
            return None

        dte = (position.contract.expiry - data_slice.date).days
        if dte < 0:
            dte = 0  # expired — force DTE stop evaluation at zero

        as_of = datetime.combine(data_slice.date, time(16, 0), tzinfo=timezone.utc)

        spot_gex = self._parse_spot_gex(position.ticker, data_slice)
        current_setup = self._current_gex_setup(position.ticker, current_price, spot_gex)
        context = self._current_exit_context(position.ticker, spot_gex, data_slice)

        return self._exit_monitor.evaluate(
            position.as_exit_position(),
            current_price=current_price,
            current_premium=current_premium,
            dte=dte,
            as_of=as_of,
            current_setup=current_setup,
            context=context,
        )

    @staticmethod
    def _parse_spot_gex(ticker: str, data_slice: BacktestDataSlice) -> list[SpotGEXByStrike]:
        raw = data_slice.spot_gex_raw.get(ticker)
        if not raw:
            return []
        try:
            return parse_spot_gex_by_strike(raw)
        except Exception:
            return []

    def _current_gex_setup(
        self,
        ticker: str,
        spot_price: Decimal,
        spot_gex: list[SpotGEXByStrike],
    ) -> GEXSetup | None:
        """Re-derive the live GEXSetup for this day, the backtest analogue of
        ExitLoop._current_gex_setup(). Lets THESIS_INVALIDATED fire in replay
        the same way it does live, instead of being structurally unreachable."""
        if not spot_gex:
            return None
        try:
            return self._detector.detect(ticker, spot_gex, spot_price)
        except Exception:
            return None

    @staticmethod
    def _current_exit_context(
        ticker: str,
        spot_gex: list[SpotGEXByStrike],
        data_slice: BacktestDataSlice,
    ) -> ExitContext:
        """Backtest analogue of ExitLoop._current_exit_context() — built from
        the same captured fixtures the entry pipeline already reads, so it's
        available for every historical day with no new capture work."""
        interpolated_iv = []
        try:
            iv_raw = data_slice.interpolated_iv_raw.get(ticker)
            if iv_raw:
                interpolated_iv = parse_interpolated_iv(iv_raw)
        except Exception:
            pass

        technicals: dict[str, list] = {}
        for fn in ("RSI", "MACD"):
            try:
                raw = data_slice.technicals_raw.get(ticker, {}).get(fn)
                technicals[fn] = parse_technical_indicator(raw, fn) if raw else []
            except Exception:
                technicals[fn] = []

        return ExitContext(spot_gex=spot_gex, interpolated_iv=interpolated_iv, technicals=technicals)
