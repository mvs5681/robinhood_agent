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
    run_id: str | None = None  # links back to telemetry events for this decision

    def detail(self) -> dict:
        """Full structured payload including all decision signals."""
        c = self.candidate
        sc = c.selected_contract
        bs = c.blend_scores
        gs = c.gex_setup
        ft = c.flow_trigger
        base = self.summary()
        base.update({
            "run_id": self.run_id,
            "blend_scores": {
                "composite": bs.composite,
                "market_tide": bs.market_tide,
                "darkpool": bs.darkpool,
                "flow_pressure": bs.flow_pressure,
                "iv_cost": bs.iv_cost,
                "technicals": bs.technicals,
            } if bs else None,
            "gex_setup": {
                "regime": gs.regime.value,
                "direction": gs.candidate_direction,
                "setup_type": gs.setup_type,
                "confidence": gs.structure_confidence,
                "flip_point": float(gs.flip_point) if gs.flip_point else None,
                "target_level": float(gs.target_level) if gs.target_level else None,
                "call_wall": float(gs.nearest_call_wall.strike) if gs.nearest_call_wall else None,
                "put_wall": float(gs.nearest_put_wall.strike) if gs.nearest_put_wall else None,
                "spot_price": float(gs.spot_price),
            } if gs else None,
            "flow_trigger": {
                "total_premium": float(ft.total_premium),
                "strike": float(ft.strike),
                "expiry": ft.expiry.isoformat(),
                "type": ft.type,
                "has_sweep": ft.has_sweep,
                "has_floor": ft.has_floor,
                "alert_rule": ft.alert_rule,
                "volume": ft.volume,
                "open_interest": ft.open_interest,
            } if ft else None,
            "contract": {
                "strike": float(sc.strike),
                "expiry": sc.expiry.isoformat(),
                "type": sc.type,
                "delta": float(sc.delta) if sc.delta else None,
                "gamma": float(sc.gamma) if sc.gamma else None,
                "theta": float(sc.theta) if sc.theta else None,
                "vega": float(sc.vega) if sc.vega else None,
                "iv": float(sc.implied_volatility) if sc.implied_volatility else None,
                "bid": float(sc.bid),
                "ask": float(sc.ask),
                "mid": float(sc.mid),
                "volume": sc.volume,
                "open_interest": sc.open_interest,
                "spread_pct": float((sc.ask - sc.bid) / sc.mid) if sc.mid else None,
            } if sc else None,
        })
        return base

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
    RETENTION_SECONDS = 3600  # decided/expired proposals dropped after 1 h

    def __init__(self) -> None:
        self._proposals: dict[str, Proposal] = {}
        self._lock = asyncio.Lock()

    def _prune(self) -> None:
        """Drop decided/expired proposals past retention. Caller must hold the lock.

        Decision history lives in telemetry — this store only needs entries
        that can still be acted on or re-displayed shortly after deciding.
        """
        now = datetime.now(timezone.utc)
        stale = [
            pid for pid, p in self._proposals.items()
            if p.status != "pending"
            and (now - (p.decided_at or p.created_at)).total_seconds() > self.RETENTION_SECONDS
        ]
        for pid in stale:
            del self._proposals[pid]

    async def add(self, candidate: CandidateSignal, run_id: str | None = None) -> Proposal:
        proposal = Proposal(
            proposal_id=str(uuid.uuid4()),
            candidate=candidate,
            created_at=datetime.now(timezone.utc),
            run_id=run_id,
        )
        async with self._lock:
            self._prune()
            self._proposals[proposal.proposal_id] = proposal
        return proposal

    async def get(self, proposal_id: str) -> Proposal | None:
        async with self._lock:
            return self._proposals.get(proposal_id)

    async def list_pending(self) -> list[Proposal]:
        now = datetime.now(timezone.utc)
        async with self._lock:
            self._prune()
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
        """Transition pending → approved. Returns None unless this call made the
        transition (unknown id, already decided, or past TTL), so callers can't
        double-execute on a repeated approval."""
        async with self._lock:
            p = self._proposals.get(proposal_id)
            if p is None or p.status != "pending":
                return None
            age = (datetime.now(timezone.utc) - p.created_at).total_seconds()
            if age > self.TTL_SECONDS:
                p.status = "expired"
                return None
            p.status = "approved"
            p.decided_at = datetime.now(timezone.utc)
            return p

    async def reject(self, proposal_id: str, note: str = "") -> Proposal | None:
        """Transition pending → rejected. Returns None unless this call made the transition."""
        async with self._lock:
            p = self._proposals.get(proposal_id)
            if p is None or p.status != "pending":
                return None
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
