from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, field_validator

from trader.gex.schemas import GEXSetup
from trader.uw.schemas import FlowAlert, OptionContract

ExecutionStatus = Literal[
    "proposed",
    "pending_approval",
    "executed",
    "skipped_no_flow",
    "skipped_risk_gate",
    "skipped_no_structure",
    "not_executable_long_only",
    "rejected_by_approval",
]

WEIGHT_KEYS = frozenset(["market_tide", "darkpool", "flow_pressure", "iv_cost", "technicals"])


class BlendScores(BaseModel):
    market_tide: float      # 0-1; direction alignment with net market flow
    darkpool: float         # 0-1; institutional darkpool accumulation pressure
    flow_pressure: float    # 0-1; directional alert fraction + net-prem momentum
    iv_cost: float          # 0-1; 1 = cheap vol (low IV percentile)
    technicals: float       # 0-1; RSI + MACD timing alignment
    composite: float        # weighted sum

    @field_validator("market_tide", "darkpool", "flow_pressure", "iv_cost", "technicals", "composite")
    @classmethod
    def clamp_to_unit(cls, v: float) -> float:
        return max(0.0, min(1.0, v))


class CandidateSignal(BaseModel):
    ticker: str
    as_of: datetime
    gex_setup: GEXSetup
    blend_scores: BlendScores
    rank: int = 0
    flow_confirmed: bool = False
    flow_trigger: FlowAlert | None = None
    selected_contract: OptionContract | None = None
    execution_status: ExecutionStatus = "proposed"
    skip_reason: str | None = None
