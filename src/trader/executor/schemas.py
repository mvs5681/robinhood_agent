from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from trader.scoring.schemas import CandidateSignal


class ExecutionMode(str, Enum):
    PROPOSE_ONLY = "propose_only"
    RH_APPROVAL = "rh_approval"
    AUTONOMOUS = "autonomous"


class OrderRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    candidate: CandidateSignal
    action: Literal["buy_to_open", "sell_to_close"]
    quantity: int
    limit_price: Decimal | None  # None = market; always set a limit for options
    mode: ExecutionMode
    ref_id: str = Field(default_factory=lambda: str(uuid4()))


class OrderResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    request: OrderRequest
    placed: bool
    order_id: str | None = None
    rejection_reason: str | None = None
    review_summary: str | None = None
    timestamp: datetime
