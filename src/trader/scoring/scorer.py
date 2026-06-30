"""
BlendScorer — Phase 3.

Computes a weighted composite score for a GEXSetup using 5 pre-fetched signals.
Fully synchronous — all data is already on state when this runs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence

from pydantic import ValidationError

from trader.gex.schemas import GEXRegime, GEXSetup
from trader.uw.schemas import (
    DarkpoolPrint,
    FlowAlert,
    InterpolatedIVEntry,
    MarketTide,
    NetPremTick,
    TechnicalPoint,
)

from .features import (
    darkpool_score,
    flow_pressure_score,
    iv_cost_score,
    market_tide_score,
    technicals_score,
)
from .schemas import BlendScores, CandidateSignal, WEIGHT_KEYS

DEFAULT_WEIGHTS: dict[str, float] = {
    "market_tide":   0.20,
    "darkpool":      0.20,
    "flow_pressure": 0.20,
    "iv_cost":       0.20,
    "technicals":    0.20,
}


class BlendScorer:
    def __init__(
        self,
        weights: dict[str, float] | None = None,
        darkpool_cap: Decimal = Decimal("5_000_000"),
    ) -> None:
        w = weights or DEFAULT_WEIGHTS
        self._validate_weights(w)
        self.weights = w
        self.darkpool_cap = darkpool_cap

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score(
        self,
        setup: GEXSetup,
        market_tide: Sequence[MarketTide],
        darkpool: Sequence[DarkpoolPrint],
        flow_alerts: Sequence[FlowAlert],
        net_prem_ticks: Sequence[NetPremTick],
        iv_entries: Sequence[InterpolatedIVEntry],
        rsi_data: Sequence[TechnicalPoint],
        macd_data: Sequence[TechnicalPoint],
    ) -> CandidateSignal:
        """
        Score a single GEXSetup and return a CandidateSignal.
        Setups with regime=MIXED or direction='none' are immediately marked as
        skipped_no_structure without computing blend scores.
        """
        if setup.regime == GEXRegime.MIXED or setup.candidate_direction == "none":
            return CandidateSignal(
                ticker=setup.ticker,
                as_of=datetime.now(timezone.utc),
                gex_setup=setup,
                blend_scores=BlendScores(
                    market_tide=0.0,
                    darkpool=0.0,
                    flow_pressure=0.0,
                    iv_cost=0.0,
                    technicals=0.0,
                    composite=0.0,
                ),
                execution_status="skipped_no_structure",
                skip_reason=f"regime={setup.regime} direction={setup.candidate_direction}",
            )

        direction = setup.candidate_direction

        mt = market_tide_score(market_tide, direction)
        dp = darkpool_score(darkpool, self.darkpool_cap)
        fp = flow_pressure_score(flow_alerts, net_prem_ticks, setup.ticker, direction)
        iv = iv_cost_score(iv_entries)
        tech = technicals_score(rsi_data, macd_data, direction)

        composite = (
            self.weights["market_tide"]   * mt
            + self.weights["darkpool"]      * dp
            + self.weights["flow_pressure"] * fp
            + self.weights["iv_cost"]       * iv
            + self.weights["technicals"]    * tech
        )

        return CandidateSignal(
            ticker=setup.ticker,
            as_of=datetime.now(timezone.utc),
            gex_setup=setup,
            blend_scores=BlendScores(
                market_tide=mt,
                darkpool=dp,
                flow_pressure=fp,
                iv_cost=iv,
                technicals=tech,
                composite=composite,
            ),
        )

    def rank(self, candidates: list[CandidateSignal]) -> list[CandidateSignal]:
        """
        Sort candidates by composite score descending and assign rank 1..N.
        Skipped candidates are placed at the end and receive rank 0.
        """
        active = [c for c in candidates if c.execution_status == "proposed"]
        skipped = [c for c in candidates if c.execution_status != "proposed"]

        active.sort(key=lambda c: c.blend_scores.composite, reverse=True)

        for i, c in enumerate(active, start=1):
            # CandidateSignal is a Pydantic model — use model_copy to avoid mutation
            active[i - 1] = c.model_copy(update={"rank": i})

        return active + skipped

    # ------------------------------------------------------------------

    @staticmethod
    def _validate_weights(w: dict[str, float]) -> None:
        missing = WEIGHT_KEYS - w.keys()
        extra = w.keys() - WEIGHT_KEYS
        if missing:
            raise ValueError(f"Missing weight keys: {missing}")
        if extra:
            raise ValueError(f"Unknown weight keys: {extra}")
        total = sum(w.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"Weights must sum to 1.0, got {total:.6f}")
