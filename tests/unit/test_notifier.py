"""Tests for TelegramNotifier message rendering.

Uses real pydantic models (not MagicMock) so attribute-name drift between
the schemas and the notifier's formatting code fails loudly — a `.direction`
vs `.candidate_direction` mismatch silently killed all proposal
notifications in production.
"""

from __future__ import annotations

from datetime import datetime, date, timezone
from decimal import Decimal

from trader.gex.schemas import GEXRegime, GEXSetup
from trader.live.notifier import TelegramNotifier
from trader.live.proposals import Proposal
from trader.scoring.schemas import BlendScores, CandidateSignal
from trader.uw.schemas import OptionContract


def _make_proposal() -> Proposal:
    contract = OptionContract(
        ticker="NVDA", expiry=date(2026, 8, 7), strike=Decimal("180"),
        type="call", bid=Decimal("4.90"), ask=Decimal("5.10"),
        open_interest=5000, volume=1000, delta=Decimal("0.38"),
    )
    setup = GEXSetup(
        ticker="NVDA",
        as_of=datetime(2026, 7, 14, 14, 0, tzinfo=timezone.utc),
        spot_price=Decimal("175"),
        regime=GEXRegime.NEGATIVE,
        flip_point=Decimal("170"),
        nearest_call_wall=None,
        nearest_put_wall=None,
        target_level=Decimal("185"),
        candidate_direction="call",
        setup_type="momentum",
        structure_confidence=0.6,
        raw_gex_by_strike=[],
    )
    candidate = CandidateSignal(
        ticker="NVDA",
        as_of=datetime(2026, 7, 14, 14, 0, tzinfo=timezone.utc),
        gex_setup=setup,
        blend_scores=BlendScores(
            market_tide=0.6, darkpool=0.7, flow_pressure=0.6,
            iv_cost=0.65, technicals=0.7, composite=0.65,
        ),
        execution_status="proposed",
        selected_contract=contract,
    )
    return Proposal(
        proposal_id="test-proposal-id",
        candidate=candidate,
        created_at=datetime.now(timezone.utc),
    )


class TestProposalText:
    def test_renders_with_real_models(self):
        notifier = TelegramNotifier("token", "chat")
        text = notifier._proposal_text(_make_proposal(), status="pending")
        assert "NVDA" in text
        assert "call" in text
        assert "negative" in text          # regime enum rendered as its value
        assert "GEXRegime" not in text     # not the raw enum repr
        assert "0.65" in text              # composite score
        assert "test-proposal-id" in text

    def test_renders_every_status_variant(self):
        notifier = TelegramNotifier("token", "chat")
        p = _make_proposal()
        for status in ("pending", "approved", "executing", "rejected", "executed",
                       "error — boom"):
            assert notifier._proposal_text(p, status=status)

    def test_renders_without_contract_or_scores(self):
        notifier = TelegramNotifier("token", "chat")
        p = _make_proposal()
        p.candidate = p.candidate.model_copy(
            update={"selected_contract": None, "blend_scores": None}
        )
        text = notifier._proposal_text(p, status="pending")
        assert "NVDA" in text
