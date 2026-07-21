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
from trader.exits.schemas import ExitSignal
from trader.gex.schemas import GEXDetectorParams
from trader.graph.agent import run_pipeline
from trader.risk.schemas import RiskParams
from trader.scoring.schemas import CandidateSignal

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

        return self._exit_monitor.evaluate(
            position.as_exit_position(),
            current_price=current_price,
            current_premium=current_premium,
            dte=dte,
            as_of=as_of,
        )
