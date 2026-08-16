"""Tests for make_position() — the fill-to-Position path used by the
autonomous watcher, notifier approval flow, and the dashboard's manual
approval handler (order_manager.py::_promote() is the separate fill-
reconciliation path, covered in test_order_manager.py)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from trader.executor.schemas import ExecutionMode, OrderRequest, OrderResult
from trader.gex.schemas import GEXRegime, GEXSetup
from trader.live.position_store import make_position
from trader.scoring.schemas import BlendScores, CandidateSignal
from trader.uw.schemas import OptionContract

AS_OF = datetime(2026, 6, 30, 15, 0, 0, tzinfo=timezone.utc)


def _candidate() -> CandidateSignal:
    setup = GEXSetup(
        ticker="AAPL", as_of=AS_OF, spot_price=Decimal("195"), regime=GEXRegime.POSITIVE,
        flip_point=None, nearest_call_wall=None, nearest_put_wall=None,
        target_level=Decimal("200"), candidate_direction="call", setup_type="pin",
        structure_confidence=0.8, raw_gex_by_strike=[],
    )
    contract = OptionContract(
        ticker="AAPL", expiry=date(2026, 7, 25), strike=Decimal("200"), type="call",
        bid=Decimal("2.90"), ask=Decimal("3.10"), open_interest=9000, volume=4500,
    )
    return CandidateSignal(
        ticker="AAPL", as_of=AS_OF, gex_setup=setup,
        blend_scores=BlendScores(market_tide=0.7, darkpool=0.8, flow_pressure=0.7,
                                  iv_cost=0.6, technicals=0.75, composite=0.71),
        execution_status="proposed", selected_contract=contract,
    )


def _order_result(candidate: CandidateSignal) -> OrderResult:
    request = OrderRequest(
        candidate=candidate, action="buy_to_open", quantity=1,
        limit_price=Decimal("3.00"), mode=ExecutionMode.PROPOSE_ONLY,
    )
    return OrderResult(request=request, placed=True, order_id="order-1", timestamp=AS_OF)


class TestMakePosition:
    def test_populates_entry_gex_setup_from_candidate(self):
        candidate = _candidate()
        pos = make_position(candidate, _order_result(candidate), quantity=1)

        assert pos is not None
        assert pos.entry_gex_setup is candidate.gex_setup

    def test_entry_regime_and_setup_type_still_populated(self):
        candidate = _candidate()
        pos = make_position(candidate, _order_result(candidate), quantity=1)

        assert pos.entry_regime == "positive"
        assert pos.entry_setup_type == "pin"

    def test_returns_none_when_no_selected_contract(self):
        candidate = _candidate().model_copy(update={"selected_contract": None})
        assert make_position(candidate, _order_result(candidate), quantity=1) is None
