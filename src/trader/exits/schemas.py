from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict

from trader.uw.schemas import OptionContract


class ExitReason(str, Enum):
    PROFIT_TARGET = "profit_target"
    THESIS_INVALIDATED = "thesis_invalidated"
    TRAILING_STOP = "trailing_stop"
    STOP_LOSS = "stop_loss"
    DTE_STOP = "dte_stop"
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
