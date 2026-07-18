from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from trader.scoring.schemas import CandidateSignal
from trader.uw.schemas import FlowAlert


class FlowTrigger:
    """
    Confirmation gate: a CandidateSignal advances only when a same-direction
    whale print (premium ≥ min_premium) exists within the lookback window.
    """

    def __init__(
        self,
        min_premium: Decimal = Decimal("100_000"),
        lookback_hours: int = 4,
    ) -> None:
        self.min_premium = min_premium
        self.lookback_hours = lookback_hours

    def check(
        self,
        candidate: CandidateSignal,
        flow_alerts: list[FlowAlert],
        as_of: datetime | None = None,
    ) -> CandidateSignal:
        """
        Return an updated copy of the candidate.
        - Already-skipped candidates (any status != "proposed") pass through.
        - "none" direction is always skipped — no flow can confirm it.
        - First matching alert by highest total_premium wins.
        """
        if candidate.execution_status != "proposed":
            return candidate

        direction = candidate.gex_setup.candidate_direction
        if direction == "none":
            return candidate.model_copy(update={
                "flow_confirmed": False,
                "execution_status": "skipped_no_flow",
                "skip_reason": "candidate_direction is none — no flow can confirm",
            })

        cutoff = (as_of or datetime.now(timezone.utc)) - timedelta(hours=self.lookback_hours)

        matching = [
            a for a in flow_alerts
            if a.ticker == candidate.ticker
            and a.type == direction
            and a.total_premium >= self.min_premium
            and a.created_at is not None
            and a.created_at >= cutoff
        ]

        if not matching:
            return candidate.model_copy(update={
                "flow_confirmed": False,
                "execution_status": "skipped_no_flow",
                "skip_reason": (
                    f"no {direction} flow alert ≥${self.min_premium:,.0f} "
                    f"in last {self.lookback_hours}h for {candidate.ticker}"
                ),
            })

        best = max(matching, key=lambda a: a.total_premium)
        return candidate.model_copy(update={
            "flow_confirmed": True,
            "flow_trigger": best,
        })
