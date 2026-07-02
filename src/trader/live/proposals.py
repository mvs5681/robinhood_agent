"""In-memory proposal store — pending orders awaiting human approval."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from trader.executor.schemas import OrderResult
    from trader.scoring.schemas import CandidateSignal


ProposalStatus = Literal["pending", "approved", "rejected", "executed", "expired"]


@dataclass
class Proposal:
    proposal_id: str
    candidate: CandidateSignal
    created_at: datetime
    status: ProposalStatus = "pending"
    decided_at: datetime | None = None
    order_result: OrderResult | None = None
    rejection_note: str | None = None

    def summary(self) -> dict:
        c = self.candidate
        sc = c.selected_contract
        return {
            "proposal_id": self.proposal_id,
            "status": self.status,
            "ticker": c.ticker,
            "action": "buy_to_open",
            "strike": float(sc.strike) if sc else None,
            "expiry": sc.expiry.isoformat() if sc else None,
            "type": sc.type if sc else None,
            "limit_price": float(sc.mid) if sc else None,
            "composite_score": c.blend_scores.composite,
            "regime": c.gex_setup.regime.value,
            "setup_type": c.gex_setup.setup_type,
            "target_level": float(c.gex_setup.target_level) if c.gex_setup.target_level else None,
            "flow_premium": float(c.flow_trigger.total_premium) if c.flow_trigger else None,
            "created_at": self.created_at.isoformat(),
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
        }


class ProposalStore:
    """
    Thread-safe store for proposals pending human approval.

    Proposals expire automatically after TTL_SECONDS (default 30 min) to
    prevent stale entries from being approved hours after market conditions changed.
    """

    TTL_SECONDS = 1800  # 30 minutes

    def __init__(self) -> None:
        self._proposals: dict[str, Proposal] = {}
        self._lock = asyncio.Lock()

    async def add(self, candidate: CandidateSignal) -> Proposal:
        proposal = Proposal(
            proposal_id=str(uuid.uuid4()),
            candidate=candidate,
            created_at=datetime.now(timezone.utc),
        )
        async with self._lock:
            self._proposals[proposal.proposal_id] = proposal
        return proposal

    async def get(self, proposal_id: str) -> Proposal | None:
        async with self._lock:
            return self._proposals.get(proposal_id)

    async def list_pending(self) -> list[Proposal]:
        now = datetime.now(timezone.utc)
        async with self._lock:
            out = []
            for p in self._proposals.values():
                if p.status == "pending":
                    age = (now - p.created_at).total_seconds()
                    if age > self.TTL_SECONDS:
                        p.status = "expired"
                    else:
                        out.append(p)
            return out

    async def approve(self, proposal_id: str) -> Proposal | None:
        async with self._lock:
            p = self._proposals.get(proposal_id)
            if p and p.status == "pending":
                p.status = "approved"
                p.decided_at = datetime.now(timezone.utc)
            return p

    async def reject(self, proposal_id: str, note: str = "") -> Proposal | None:
        async with self._lock:
            p = self._proposals.get(proposal_id)
            if p and p.status == "pending":
                p.status = "rejected"
                p.decided_at = datetime.now(timezone.utc)
                p.rejection_note = note
            return p

    async def mark_executed(self, proposal_id: str, result: OrderResult) -> None:
        async with self._lock:
            p = self._proposals.get(proposal_id)
            if p:
                p.status = "executed"
                p.order_result = result
