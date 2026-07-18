from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from trader.scoring.schemas import CandidateSignal
from trader.uw.schemas import OptionContract


class SelectorParams(BaseModel):
    dte_min: int = 21
    dte_max: int = 30
    delta_min: float = 0.30
    delta_max: float = 0.45


class ContractSelector:
    """
    Picks the single best OptionContract for a flow-confirmed CandidateSignal.

    Filtering order:
      1. Direction match (call/put)
      2. DTE in [dte_min, dte_max]
      3. |delta| in [delta_min, delta_max]

    Among eligible contracts, sorts by:
      (distance_to_target_level ASC, spread_pct ASC, open_interest DESC)

    Returns the candidate unchanged (with execution_status="not_executable_long_only")
    if no contract survives the filters.
    """

    def __init__(self, params: SelectorParams | None = None) -> None:
        self.params = params or SelectorParams()

    def select(
        self,
        candidate: CandidateSignal,
        contracts: list[OptionContract],
    ) -> CandidateSignal:
        if candidate.execution_status != "proposed":
            return candidate

        direction = candidate.gex_setup.candidate_direction
        # Use the GEXSetup timestamp as the reference date so tests are time-independent
        today: date = candidate.gex_setup.as_of.date()
        p = self.params

        eligible = [
            c for c in contracts
            if c.type == direction
            and p.dte_min <= (c.expiry - today).days <= p.dte_max
            and c.delta is not None
            and p.delta_min <= abs(float(c.delta)) <= p.delta_max
        ]

        if not eligible:
            return candidate.model_copy(update={
                "execution_status": "not_executable_long_only",
                "skip_reason": (
                    f"no {direction} contract with DTE {p.dte_min}–{p.dte_max} "
                    f"and |delta| {p.delta_min}–{p.delta_max}"
                ),
            })

        target = candidate.gex_setup.target_level

        def _sort_key(c: OptionContract) -> tuple:
            distance = abs(float(c.strike - target)) if target is not None else 0.0
            return (distance, float(c.spread_pct), -c.open_interest)

        eligible.sort(key=_sort_key)
        return candidate.model_copy(update={"selected_contract": eligible[0]})
