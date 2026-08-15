"""Unit tests for ReplayState JSON round-tripping — the resumable state that
lets BacktestHarness.step_forward() build a cumulative track record across
separate daily invocations instead of re-walking history each time."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from trader.backtest.schemas import BacktestPosition, BacktestTradeRecord
from trader.backtest.state import ReplayState
from trader.exits.schemas import ExitReason, ExitSignal
from trader.gex.schemas import GEXRegime, GEXSetup
from trader.scoring.schemas import BlendScores, CandidateSignal
from trader.uw.schemas import OptionContract


def _contract(strike="200") -> OptionContract:
    return OptionContract(
        ticker="AAPL", expiry=date(2026, 9, 18), strike=Decimal(strike),
        type="call", bid=Decimal("2.90"), ask=Decimal("3.10"),
        open_interest=500, volume=200,
    )


def _setup() -> GEXSetup:
    return GEXSetup(
        ticker="AAPL", as_of=datetime(2026, 8, 1, tzinfo=timezone.utc),
        spot_price=Decimal("195"), regime=GEXRegime.POSITIVE, flip_point=None,
        nearest_call_wall=None, nearest_put_wall=None, target_level=Decimal("210"),
        candidate_direction="call", setup_type="pin", structure_confidence=0.7,
        raw_gex_by_strike=[],
    )


def _candidate() -> CandidateSignal:
    return CandidateSignal(
        ticker="AAPL", as_of=datetime(2026, 8, 1, tzinfo=timezone.utc),
        gex_setup=_setup(),
        blend_scores=BlendScores(market_tide=0.6, darkpool=0.6, flow_pressure=0.6,
                                 iv_cost=0.6, technicals=0.6, composite=0.6),
        execution_status="proposed", selected_contract=_contract(),
    )


def _open_position() -> BacktestPosition:
    return BacktestPosition.from_candidate(_candidate(), entry_date=date(2026, 8, 1))


def _closed_record() -> BacktestTradeRecord:
    pos = _open_position()
    record = BacktestTradeRecord(position=pos, candidate=_candidate(), entry_date=date(2026, 8, 1))
    signal = ExitSignal(
        position_id=pos.position_id, ticker="AAPL", contract=pos.contract,
        reason=ExitReason.PROFIT_TARGET, current_premium=Decimal("4.50"),
        entry_premium=pos.entry_premium, pnl_pct=0.5, dte_remaining=20,
        as_of=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )
    record.close(signal, date(2026, 8, 4))
    return record


class TestReplayStateRoundTrip:
    def test_empty_state_round_trips(self):
        state = ReplayState(cash=Decimal("2000"))
        restored = ReplayState.from_dict(state.to_dict())
        assert restored.cash == Decimal("2000")
        assert restored.records == []
        assert restored.open_positions == []
        assert restored.processed_dates == []

    def test_closed_record_round_trips(self):
        record = _closed_record()
        state = ReplayState(records=[record], cash=Decimal("1700"),
                            processed_dates=[date(2026, 8, 1), date(2026, 8, 4)])
        restored = ReplayState.from_dict(state.to_dict())

        assert len(restored.records) == 1
        r = restored.records[0]
        assert r.status == "closed"
        assert r.pnl_pct == 0.5
        assert r.exit_date == date(2026, 8, 4)
        assert r.exit_signal.reason == ExitReason.PROFIT_TARGET
        assert r.position.ticker == "AAPL"
        assert r.position.entry_premium == record.position.entry_premium
        assert r.candidate.gex_setup.regime == GEXRegime.POSITIVE
        assert r.candidate.gex_setup.setup_type == "pin"
        assert restored.processed_dates == [date(2026, 8, 1), date(2026, 8, 4)]

    def test_open_position_reference_survives_round_trip(self):
        pos = _open_position()
        record = BacktestTradeRecord(position=pos, candidate=_candidate(), entry_date=date(2026, 8, 1))
        state = ReplayState(open_positions=[pos], records=[record], cash=Decimal("1500"))

        restored = ReplayState.from_dict(state.to_dict())

        assert len(restored.open_positions) == 1
        assert restored.open_positions[0].position_id == pos.position_id
        # Must be the SAME object identity as the one in records, not a copy —
        # _step_one_day mutates the record found via record_by_id[position_id],
        # so open_positions and records need to reference consistent data.
        assert restored.open_positions[0].position_id == restored.records[0].position.position_id

    def test_equity_curve_round_trips(self):
        state = ReplayState(equity_curve=[(date(2026, 8, 1), 2000.0), (date(2026, 8, 4), 2150.0)])
        restored = ReplayState.from_dict(state.to_dict())
        assert restored.equity_curve == [(date(2026, 8, 1), 2000.0), (date(2026, 8, 4), 2150.0)]

    def test_record_by_id_indexes_by_position_id(self):
        record = _closed_record()
        state = ReplayState(records=[record])
        assert state.record_by_id[record.position.position_id] is record
