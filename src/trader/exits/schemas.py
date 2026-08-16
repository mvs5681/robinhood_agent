from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from trader.gex.schemas import GEXSetup
from trader.uw.schemas import InterpolatedIVEntry, OptionContract, SpotGEXByStrike, TechnicalPoint


class ExitReason(str, Enum):
    PROFIT_TARGET = "profit_target"
    THESIS_INVALIDATED = "thesis_invalidated"
    TRAILING_STOP = "trailing_stop"
    STOP_LOSS = "stop_loss"
    DTE_STOP = "dte_stop"
    EARNINGS_GAP = "earnings_gap"
    MANUAL = "manual"


class Position(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    position_id: str
    ticker: str
    contract: OptionContract
    entry_premium: Decimal          # option mid at entry (per share)
    target_level: Decimal | None    # GEX gamma wall — None for reconciled positions
    opened_at: datetime
    quantity: int = 1
    option_instrument_id: str | None = None   # cached RH instrument UUID
    sector: str | None = None
    peak_premium: Decimal | None = None  # highest premium observed since entry — updated
                                         # by ExitLoop each tick, drives the trailing stop
    # GEXSetup context at entry — None for reconciled/adopted positions where
    # the originating CandidateSignal isn't available. Carried through to
    # exit telemetry so live trades can be sliced by regime/setup_type the
    # same way backtest trades already are, for backtest-vs-reality comparison.
    entry_regime: str | None = None
    entry_setup_type: str | None = None
    entry_gex_setup: GEXSetup | None = None  # GEXSetup snapshotted at fill time — the
                                              # "then" side of the thesis-confidence-decay
                                              # drift check (None for reconciled/adopted
                                              # positions that never went through the
                                              # pipeline, same as target_level)


class ExitContext(BaseModel):
    """
    Live market context fetched fresh each tick, for exit rules that need more
    than spot/premium/dte — the dynamic signals a static threshold can't see.

    Everything here is best-effort: missing/empty means "not available this
    tick" (stale cache, thin data), never an error. Distinct from
    current_setup (already a direct evaluate() argument, unchanged) — this
    carries the raw signals current_setup is built from, plus IV/technicals,
    for gates that need more than the coarse regime/direction call.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    spot_gex: list[SpotGEXByStrike] = Field(default_factory=list)
    interpolated_iv: list[InterpolatedIVEntry] = Field(default_factory=list)
    technicals: dict[str, list["TechnicalPoint"]] = Field(default_factory=dict)

    def iv_percentile_at(self, dte: int) -> Decimal | None:
        """IV percentile (0-100) at the interpolated-IV horizon closest to dte."""
        if not self.interpolated_iv:
            return None
        closest = min(self.interpolated_iv, key=lambda e: abs(e.days - dte))
        return closest.percentile

    def rsi_latest(self) -> Decimal | None:
        rows = self.technicals.get("RSI")
        if not rows:
            return None
        return sorted(rows, key=lambda r: r.timestamp)[-1].value

    def macd_histogram_latest(self) -> Decimal | None:
        rows = self.technicals.get("MACD")
        if not rows:
            return None
        return sorted(rows, key=lambda r: r.timestamp)[-1].histogram


class ExitSignal(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    position_id: str
    ticker: str
    contract: OptionContract
    reason: ExitReason
    current_premium: Decimal
    entry_premium: Decimal
    pnl_pct: float              # (current - entry) / entry
    dte_remaining: int
    as_of: datetime
