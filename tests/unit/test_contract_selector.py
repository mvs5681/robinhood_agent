"""
Unit tests for Phase 5: ContractSelector.

as_of is derived from candidate.gex_setup.as_of, which comes from GEXDetector
and uses the current datetime — making DTE calculations time-sensitive.
We override as_of by constructing GEXSetup with a fixed datetime of 2026-06-30
so that fixture expiry dates (2026-07-25 = 25 DTE, etc.) are always stable.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from trader.contracts.selector import ContractSelector, SelectorParams
from trader.scoring.schemas import CandidateSignal
from trader.scoring.scorer import BlendScorer
from trader.uw.schemas import OptionContract
from trader.uw.validators import parse_option_contracts, parse_spot_gex_by_strike


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

FIXED_AS_OF = datetime(2026, 6, 30, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def contracts(tmp_path):
    """Load AAPL option contracts from the standard fixture."""
    import json
    from pathlib import Path
    raw = json.loads(
        (Path(__file__).parent.parent / "fixtures" / "AAPL_option_contracts.json").read_text()
    )
    return parse_option_contracts(raw)


@pytest.fixture
def proposed_call_candidate(gex_positive_raw):
    """
    A proposed, flow-confirmed AAPL call candidate with a fixed as_of timestamp.
    target_level = 200 (largest call wall above spot=192 in gex_positive fixture).
    """
    from trader.gex.detector import GEXDetector

    gex = parse_spot_gex_by_strike(gex_positive_raw)
    setup = GEXDetector().detect("AAPL", gex, Decimal("192"), as_of=FIXED_AS_OF)
    candidate = BlendScorer().score(
        setup,
        market_tide=[], darkpool=[], flow_alerts=[],
        net_prem_ticks=[], iv_entries=[], rsi_data=[], macd_data=[],
    )
    # Mark as flow-confirmed so selector processes it
    return candidate.model_copy(update={"flow_confirmed": True})


@pytest.fixture
def selector():
    return ContractSelector()


def _make_contract(
    strike: str = "200.0",
    expiry: str = "2026-07-25",
    direction: str = "call",
    delta: str = "0.38",
    bid: str = "3.00",
    ask: str = "3.20",
    oi: int = 5000,
) -> OptionContract:
    return OptionContract(
        ticker="AAPL",
        expiry=date.fromisoformat(expiry),
        strike=Decimal(strike),
        type=direction,
        bid=Decimal(bid),
        ask=Decimal(ask),
        open_interest=oi,
        volume=1000,
        delta=Decimal(delta),
    )


# ---------------------------------------------------------------------------
# Pass-through: non-proposed candidates unchanged
# ---------------------------------------------------------------------------


class TestPassThrough:
    def test_skipped_no_structure_unchanged(self, gex_mixed_raw, contracts, selector):
        from trader.gex.detector import GEXDetector

        gex = parse_spot_gex_by_strike(gex_mixed_raw)
        setup = GEXDetector().detect("AAPL", gex, Decimal("192"), as_of=FIXED_AS_OF)
        skipped = BlendScorer().score(
            setup, market_tide=[], darkpool=[], flow_alerts=[],
            net_prem_ticks=[], iv_entries=[], rsi_data=[], macd_data=[],
        )
        assert skipped.execution_status == "skipped_no_structure"
        result = selector.select(skipped, contracts)
        assert result.execution_status == "skipped_no_structure"
        assert result is skipped

    def test_skipped_no_flow_unchanged(self, proposed_call_candidate, contracts, selector):
        no_flow = proposed_call_candidate.model_copy(update={
            "execution_status": "skipped_no_flow",
            "flow_confirmed": False,
        })
        result = selector.select(no_flow, contracts)
        assert result.execution_status == "skipped_no_flow"
        assert result is no_flow


# ---------------------------------------------------------------------------
# Successful selection
# ---------------------------------------------------------------------------


class TestSelection:
    def test_selects_a_contract(self, proposed_call_candidate, contracts, selector):
        result = selector.select(proposed_call_candidate, contracts)
        assert result.selected_contract is not None

    def test_selected_contract_is_correct_direction(
        self, proposed_call_candidate, contracts, selector
    ):
        result = selector.select(proposed_call_candidate, contracts)
        assert result.selected_contract.type == "call"

    def test_selected_contract_within_dte_band(
        self, proposed_call_candidate, contracts, selector
    ):
        result = selector.select(proposed_call_candidate, contracts)
        c = result.selected_contract
        dte = (c.expiry - FIXED_AS_OF.date()).days
        assert 21 <= dte <= 30

    def test_selected_contract_within_delta_band(
        self, proposed_call_candidate, contracts, selector
    ):
        result = selector.select(proposed_call_candidate, contracts)
        c = result.selected_contract
        assert 0.30 <= abs(float(c.delta)) <= 0.45

    def test_anchors_to_target_level(self, proposed_call_candidate, contracts, selector):
        # target_level = 200; closest in-band call is the 200-strike contract
        result = selector.select(proposed_call_candidate, contracts)
        assert result.selected_contract.strike == Decimal("200.0")

    def test_execution_status_stays_proposed(
        self, proposed_call_candidate, contracts, selector
    ):
        result = selector.select(proposed_call_candidate, contracts)
        assert result.execution_status == "proposed"

    def test_blend_scores_preserved(self, proposed_call_candidate, contracts, selector):
        original = proposed_call_candidate.blend_scores.composite
        result = selector.select(proposed_call_candidate, contracts)
        assert result.blend_scores.composite == original


# ---------------------------------------------------------------------------
# No eligible contract → not_executable_long_only
# ---------------------------------------------------------------------------


class TestNoEligibleContract:
    def test_empty_contracts_returns_not_executable(
        self, proposed_call_candidate, selector
    ):
        result = selector.select(proposed_call_candidate, [])
        assert result.execution_status == "not_executable_long_only"
        assert result.selected_contract is None
        assert result.skip_reason is not None

    def test_wrong_direction_only_returns_not_executable(
        self, proposed_call_candidate, selector
    ):
        puts_only = [_make_contract(direction="put", delta="-0.38")]
        result = selector.select(proposed_call_candidate, puts_only)
        assert result.execution_status == "not_executable_long_only"

    def test_dte_too_high_returns_not_executable(
        self, proposed_call_candidate, selector
    ):
        # 52 DTE > 30 max
        far_dated = [_make_contract(expiry="2026-08-21")]
        result = selector.select(proposed_call_candidate, far_dated)
        assert result.execution_status == "not_executable_long_only"

    def test_dte_too_low_returns_not_executable(
        self, proposed_call_candidate, selector
    ):
        # 8 DTE < 21 min
        near_dated = [_make_contract(expiry="2026-07-08")]
        result = selector.select(proposed_call_candidate, near_dated)
        assert result.execution_status == "not_executable_long_only"

    def test_delta_too_low_returns_not_executable(
        self, proposed_call_candidate, selector
    ):
        low_delta = [_make_contract(delta="0.29")]  # below 0.30 min
        result = selector.select(proposed_call_candidate, low_delta)
        assert result.execution_status == "not_executable_long_only"

    def test_delta_too_high_returns_not_executable(
        self, proposed_call_candidate, selector
    ):
        high_delta = [_make_contract(delta="0.46")]  # above 0.45 max
        result = selector.select(proposed_call_candidate, high_delta)
        assert result.execution_status == "not_executable_long_only"

    def test_skip_reason_mentions_direction(self, proposed_call_candidate, selector):
        result = selector.select(proposed_call_candidate, [])
        assert "call" in (result.skip_reason or "")


# ---------------------------------------------------------------------------
# Sort / tie-breaking
# ---------------------------------------------------------------------------


class TestSortOrder:
    def test_picks_closest_to_target_level(self, proposed_call_candidate, selector):
        # target_level = 200; offer 195 and 200 both in-band
        c195 = _make_contract(strike="195.0", delta="0.44")
        c200 = _make_contract(strike="200.0", delta="0.35")
        result = selector.select(proposed_call_candidate, [c195, c200])
        assert result.selected_contract.strike == Decimal("200.0")

    def test_tiebreaks_by_spread_pct(self, proposed_call_candidate, selector):
        # Two contracts at same strike — pick the tighter spread
        wide = _make_contract(strike="200.0", delta="0.35", bid="2.00", ask="2.60", oi=5000)
        tight = _make_contract(strike="200.0", delta="0.35", bid="2.90", ask="3.10", oi=5000)
        result = selector.select(proposed_call_candidate, [wide, tight])
        assert result.selected_contract.bid == Decimal("2.90")

    def test_tiebreaks_by_oi_when_spread_equal(self, proposed_call_candidate, selector):
        low_oi = _make_contract(strike="200.0", delta="0.35", bid="3.00", ask="3.20", oi=1000)
        high_oi = _make_contract(strike="200.0", delta="0.35", bid="3.00", ask="3.20", oi=9000)
        result = selector.select(proposed_call_candidate, [low_oi, high_oi])
        assert result.selected_contract.open_interest == 9000

    def test_no_target_level_falls_back_to_liquidity(self, proposed_call_candidate, selector):
        # Null out target_level
        patched_setup = proposed_call_candidate.gex_setup.model_copy(update={"target_level": None})
        candidate = proposed_call_candidate.model_copy(update={"gex_setup": patched_setup})

        tight = _make_contract(strike="200.0", delta="0.35", bid="3.00", ask="3.20", oi=9000)
        wide = _make_contract(strike="197.5", delta="0.40", bid="2.00", ask="2.80", oi=5000)
        result = selector.select(candidate, [tight, wide])
        # tight spread wins when distance is 0 for all
        assert result.selected_contract.bid == Decimal("3.00")


# ---------------------------------------------------------------------------
# Custom SelectorParams
# ---------------------------------------------------------------------------


class TestCustomParams:
    def test_custom_dte_band_applied(self, proposed_call_candidate):
        # Narrow band 22–24 excludes the 25-DTE fixture contracts
        narrow = ContractSelector(SelectorParams(dte_min=22, dte_max=24))
        result = narrow.select(proposed_call_candidate, [_make_contract(expiry="2026-07-25")])
        assert result.execution_status == "not_executable_long_only"

    def test_custom_delta_band_applied(self, proposed_call_candidate):
        tight_delta = ContractSelector(SelectorParams(delta_min=0.40, delta_max=0.45))
        # 0.35-delta contract rejected; 0.40-delta accepted
        c35 = _make_contract(strike="200.0", delta="0.35")
        c40 = _make_contract(strike="197.5", delta="0.40")
        result = tight_delta.select(proposed_call_candidate, [c35, c40])
        assert result.selected_contract is not None
        assert float(result.selected_contract.delta) == pytest.approx(0.40)

    def test_put_direction_selects_put_contract(self, gex_negative_raw, selector):
        from trader.gex.detector import GEXDetector

        gex = parse_spot_gex_by_strike(gex_negative_raw)
        # Use a spot price below the flip point so direction=put
        setup = GEXDetector().detect("AAPL", gex, Decimal("185"), as_of=FIXED_AS_OF)
        if setup.candidate_direction != "put":
            pytest.skip("negative fixture direction not put at this spot")

        candidate = BlendScorer().score(
            setup, market_tide=[], darkpool=[], flow_alerts=[],
            net_prem_ticks=[], iv_entries=[], rsi_data=[], macd_data=[],
        ).model_copy(update={"flow_confirmed": True})

        put_contract = _make_contract(
            direction="put", delta="-0.38", strike="185.0", expiry="2026-07-25"
        )
        result = selector.select(candidate, [put_contract])
        assert result.selected_contract is not None
        assert result.selected_contract.type == "put"
