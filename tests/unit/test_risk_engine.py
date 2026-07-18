"""Unit tests for Phase 6a: RiskEngine."""

from decimal import Decimal
from datetime import datetime, timezone, date

import pytest

from trader.risk.engine import RiskEngine
from trader.risk.schemas import PortfolioState, RiskParams, RiskVerdict
from trader.scoring.schemas import CandidateSignal, BlendScores
from trader.gex.schemas import GEXSetup, GEXRegime
from trader.uw.schemas import OptionContract


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_contract(mid: str = "4.00") -> OptionContract:
    bid = Decimal(mid) - Decimal("0.10")
    ask = Decimal(mid) + Decimal("0.10")
    return OptionContract(
        ticker="AAPL", expiry=date(2026, 7, 25), strike=Decimal("200"),
        type="call", bid=bid, ask=ask, open_interest=5000, volume=1000,
        delta=Decimal("0.38"),
    )


def _make_setup(ticker: str = "AAPL") -> GEXSetup:
    return GEXSetup(
        ticker=ticker,
        as_of=datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc),
        spot_price=Decimal("192"),
        regime=GEXRegime.POSITIVE,
        flip_point=None,
        nearest_call_wall=None,
        nearest_put_wall=None,
        target_level=Decimal("200"),
        candidate_direction="call",
        setup_type="pin",
        structure_confidence=0.75,
        raw_gex_by_strike=[],
    )


def _make_candidate(
    ticker: str = "AAPL",
    contract_mid: str = "4.00",
    status: str = "proposed",
    with_contract: bool = True,
) -> CandidateSignal:
    return CandidateSignal(
        ticker=ticker,
        as_of=datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc),
        gex_setup=_make_setup(ticker),
        blend_scores=BlendScores(
            market_tide=0.6, darkpool=0.7, flow_pressure=0.6,
            iv_cost=0.65, technicals=0.7, composite=0.65,
        ),
        execution_status=status,
        selected_contract=_make_contract(contract_mid) if with_contract else None,
    )


# ---------------------------------------------------------------------------
# Clean pass — all gates open
# ---------------------------------------------------------------------------


class TestAllGatesPass:
    def test_empty_portfolio_approves(self):
        engine = RiskEngine()
        verdict = engine.check(_make_candidate())
        assert verdict.approved is True
        assert verdict.reasons == []

    def test_two_open_positions_still_approves(self):
        engine = RiskEngine(portfolio=PortfolioState(open_positions=2))
        assert engine.check(_make_candidate()).approved is True

    def test_premium_at_cap_approves(self):
        # mid=$5.00 → cost=$500 = cap exactly → approved
        engine = RiskEngine()
        assert engine.check(_make_candidate(contract_mid="5.00")).approved is True


# ---------------------------------------------------------------------------
# Gate 1: position count
# ---------------------------------------------------------------------------


class TestPositionCountGate:
    def test_at_max_positions_rejected(self):
        engine = RiskEngine(portfolio=PortfolioState(open_positions=3))
        verdict = engine.check(_make_candidate())
        assert verdict.approved is False
        assert any("max_concurrent_positions" in r for r in verdict.reasons)

    def test_above_max_positions_rejected(self):
        engine = RiskEngine(portfolio=PortfolioState(open_positions=5))
        assert engine.check(_make_candidate()).approved is False

    def test_record_fill_increments_count(self):
        engine = RiskEngine(portfolio=PortfolioState(open_positions=2))
        assert engine.check(_make_candidate()).approved is True
        engine.record_fill("AAPL")
        assert engine.check(_make_candidate()).approved is False

    def test_custom_max_positions(self):
        engine = RiskEngine(
            params=RiskParams(max_concurrent_positions=5),
            portfolio=PortfolioState(open_positions=4),
        )
        assert engine.check(_make_candidate()).approved is True
        engine.record_fill("AAPL")
        assert engine.check(_make_candidate()).approved is False


# ---------------------------------------------------------------------------
# Gate 2: premium cap
# ---------------------------------------------------------------------------


class TestPremiumCapGate:
    def test_cost_above_cap_rejected(self):
        # mid=$5.01 → cost=$501 > $500 cap
        engine = RiskEngine()
        verdict = engine.check(_make_candidate(contract_mid="5.01"))
        assert verdict.approved is False
        assert any("premium cost" in r for r in verdict.reasons)

    def test_no_contract_skips_gate(self):
        # Candidate without selected_contract skips the premium gate
        engine = RiskEngine()
        candidate = _make_candidate(with_contract=False)
        assert engine.check(candidate).approved is True

    def test_custom_premium_cap(self):
        engine = RiskEngine(params=RiskParams(max_premium_per_trade=Decimal("300")))
        # mid=$3.01 → cost=$301 > $300
        verdict = engine.check(_make_candidate(contract_mid="3.01"))
        assert verdict.approved is False
        # mid=$3.00 → cost=$300 = cap → approved
        verdict = engine.check(_make_candidate(contract_mid="3.00"))
        assert verdict.approved is True


# ---------------------------------------------------------------------------
# Gate 3: kill-switch
# ---------------------------------------------------------------------------


class TestKillSwitch:
    def test_not_active_initially(self):
        engine = RiskEngine()
        assert engine.kill_switch_active is False

    def test_trips_at_threshold(self):
        # NAV=$10k, 5% = $500 loss threshold
        engine = RiskEngine(portfolio=PortfolioState(account_nav=Decimal("10000")))
        engine.record_pnl(Decimal("-500"))
        assert engine.kill_switch_active is True

    def test_does_not_trip_below_threshold(self):
        engine = RiskEngine(portfolio=PortfolioState(account_nav=Decimal("10000")))
        engine.record_pnl(Decimal("-499"))
        assert engine.kill_switch_active is False

    def test_accumulates_across_record_pnl_calls(self):
        engine = RiskEngine(portfolio=PortfolioState(account_nav=Decimal("10000")))
        engine.record_pnl(Decimal("-300"))
        assert engine.kill_switch_active is False
        engine.record_pnl(Decimal("-200"))  # cumulative: -500
        assert engine.kill_switch_active is True

    def test_check_returns_false_once_tripped(self):
        engine = RiskEngine(portfolio=PortfolioState(account_nav=Decimal("10000")))
        engine.record_pnl(Decimal("-500"))
        verdict = engine.check(_make_candidate())
        assert verdict.approved is False
        assert any("kill_switch" in r for r in verdict.reasons)

    def test_kill_switch_never_resets(self):
        engine = RiskEngine(portfolio=PortfolioState(account_nav=Decimal("10000")))
        engine.record_pnl(Decimal("-500"))
        assert engine.kill_switch_active is True
        # "recovering" the P&L should NOT reset the switch
        engine.record_pnl(Decimal("1000"))
        assert engine.kill_switch_active is True

    def test_kill_switch_blocks_regardless_of_other_gates(self):
        # Even with a clean portfolio, kill-switch overrides everything
        engine = RiskEngine(portfolio=PortfolioState(account_nav=Decimal("10000")))
        engine.record_pnl(Decimal("-600"))
        verdict = engine.check(_make_candidate())
        assert verdict.approved is False
        assert len(verdict.reasons) == 1  # only kill-switch reason

    def test_pre_tripped_by_injected_portfolio(self):
        # Inject a portfolio already at -$500 loss on $10k NAV
        portfolio = PortfolioState(
            account_nav=Decimal("10000"),
            daily_pnl=Decimal("-500"),
        )
        engine = RiskEngine(portfolio=portfolio)
        assert engine.kill_switch_active is True


# ---------------------------------------------------------------------------
# Gate 4: sector concentration
# ---------------------------------------------------------------------------


class TestSectorConcentration:
    def test_sector_at_max_rejected(self):
        engine = RiskEngine(
            portfolio=PortfolioState(sector_counts={"tech": 2}),
            sector_map={"AAPL": "tech"},
        )
        verdict = engine.check(_make_candidate("AAPL"))
        assert verdict.approved is False
        assert any("tech" in r for r in verdict.reasons)

    def test_sector_below_max_approved(self):
        engine = RiskEngine(
            portfolio=PortfolioState(sector_counts={"tech": 1}),
            sector_map={"AAPL": "tech"},
        )
        assert engine.check(_make_candidate("AAPL")).approved is True

    def test_unknown_sector_skips_gate(self):
        # Ticker not in sector_map → gate skipped
        engine = RiskEngine(
            portfolio=PortfolioState(sector_counts={"tech": 2}),
            sector_map={},  # AAPL not mapped
        )
        assert engine.check(_make_candidate("AAPL")).approved is True

    def test_record_fill_increments_sector(self):
        engine = RiskEngine(
            portfolio=PortfolioState(sector_counts={"tech": 1}),
            sector_map={"AAPL": "tech"},
        )
        assert engine.check(_make_candidate("AAPL")).approved is True
        engine.record_fill("AAPL", sector="tech")
        assert engine.check(_make_candidate("AAPL")).approved is False

    def test_different_sector_not_affected(self):
        engine = RiskEngine(
            portfolio=PortfolioState(sector_counts={"tech": 2}),
            sector_map={"AAPL": "tech", "XOM": "energy"},
        )
        # AAPL (tech) blocked; XOM (energy, 0 positions) approved
        assert engine.check(_make_candidate("AAPL")).approved is False
        assert engine.check(_make_candidate("XOM")).approved is True

    def test_custom_sector_concentration(self):
        engine = RiskEngine(
            params=RiskParams(max_sector_concentration=3),
            portfolio=PortfolioState(sector_counts={"tech": 2}),
            sector_map={"AAPL": "tech"},
        )
        assert engine.check(_make_candidate("AAPL")).approved is True
        engine.record_fill("AAPL", sector="tech")
        assert engine.check(_make_candidate("AAPL")).approved is False


# ---------------------------------------------------------------------------
# Multiple gates failing simultaneously
# ---------------------------------------------------------------------------


class TestMultipleFailures:
    def test_all_reasons_collected(self):
        engine = RiskEngine(
            portfolio=PortfolioState(
                open_positions=3,
                sector_counts={"tech": 2},
            ),
            sector_map={"AAPL": "tech"},
        )
        # Position cap + premium cap (mid=$5.01) + sector
        verdict = engine.check(_make_candidate(contract_mid="5.01"))
        assert verdict.approved is False
        assert len(verdict.reasons) >= 2

    def test_verdict_has_one_reason_per_failed_gate(self):
        engine = RiskEngine(
            portfolio=PortfolioState(open_positions=3),
        )
        verdict = engine.check(_make_candidate(contract_mid="5.01"))
        # Position cap + premium cap
        reason_text = " ".join(verdict.reasons)
        assert "max_concurrent" in reason_text
        assert "premium cost" in reason_text
