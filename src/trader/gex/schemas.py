from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel

from trader.uw.schemas import SpotGEXByStrike


class GEXRegime(str, Enum):
    POSITIVE = "positive"  # dealers suppress vol → pin / mean-revert
    NEGATIVE = "negative"  # dealers amplify vol → momentum / squeeze
    MIXED = "mixed"        # no clean structure → skip


class GEXWall(BaseModel):
    strike: Decimal
    net_gex: Decimal
    distance_pct: Decimal          # abs((wall - spot) / spot)
    side: Literal["call_wall", "put_wall", "flip_point"]


class GEXSetup(BaseModel):
    ticker: str
    as_of: datetime
    spot_price: Decimal
    regime: GEXRegime
    flip_point: Decimal | None           # strike where net GEX crosses zero
    nearest_call_wall: GEXWall | None
    nearest_put_wall: GEXWall | None
    target_level: Decimal | None         # primary exit target for the trade
    candidate_direction: Literal["call", "put", "none"]
    setup_type: Literal["pin", "squeeze", "momentum", "none"]
    structure_confidence: float          # 0–1; drives whether candidate survives to scorer
    raw_gex_by_strike: list[SpotGEXByStrike]


class GEXDetectorParams(BaseModel):
    # Confidence = min(top_3_concentration, |regime_ratio|).
    # Below this threshold the setup is classified MIXED regardless of regime_ratio.
    min_confidence_threshold: float = 0.15

    # Net GEX as fraction of total abs GEX that defines regime direction.
    positive_regime_threshold: float = 0.30
    negative_regime_threshold: float = -0.30
