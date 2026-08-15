"""Unit tests for Phase 6b: ExitMonitor."""

from datetime import datetime, timezone, date
from decimal import Decimal

import pytest

from trader.exits.monitor import ExitMonitor
from trader.exits.schemas import ExitContext, ExitReason, Position
from trader.gex.schemas import GEXRegime, GEXSetup, GEXWall
from trader.uw.schemas import InterpolatedIVEntry, OptionContract, TechnicalPoint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AS_OF = datetime(2026, 6, 30, 15, 0, 0, tzinfo=timezone.utc)

DEFAULT_MONITOR = ExitMonitor(stop_loss_pct=0.35, dte_floor=7)


def _contract() -> OptionContract:
    return OptionContract(
        ticker="AAPL", expiry=date(2026, 7, 25), strike=Decimal("200"),
        type="call", bid=Decimal("2.90"), ask=Decimal("3.10"),
        open_interest=9000, volume=4500, delta=Decimal("0.35"),
    )


def _position(
    target_level: str = "200",
    entry_premium: str = "3.00",
    peak_premium: str | None = None,
    entry_gex_setup: GEXSetup | None = None,
) -> Position:
    return Position(
        position_id="pos-001",
        ticker="AAPL",
        contract=_contract(),
        entry_premium=Decimal(entry_premium),
        target_level=Decimal(target_level),
        opened_at=datetime(2026, 6, 28, 10, 0, tzinfo=timezone.utc),
        peak_premium=Decimal(peak_premium) if peak_premium is not None else None,
        entry_gex_setup=entry_gex_setup,
    )


def _context(iv_percentile: str, dte: int = 14) -> ExitContext:
    return ExitContext(interpolated_iv=[
        InterpolatedIVEntry(days=dte, volatility=Decimal("0.3"), percentile=Decimal(iv_percentile)),
    ])


def _momentum_context(rsi: str | None, macd_histogram: str | None) -> ExitContext:
    technicals = {}
    if rsi is not None:
        technicals["RSI"] = [TechnicalPoint(timestamp="2026-06-30", value=Decimal(rsi))]
    if macd_histogram is not None:
        technicals["MACD"] = [TechnicalPoint(timestamp="2026-06-30",
                                              histogram=Decimal(macd_histogram))]
    return ExitContext(technicals=technicals)


def _wall(distance_pct: str, side: str = "call_wall", strike: str = "205") -> GEXWall:
    return GEXWall(strike=Decimal(strike), net_gex=Decimal("1000"),
                    distance_pct=Decimal(distance_pct), side=side)


def _setup(
    direction: str = "call",
    regime: GEXRegime = GEXRegime.NEGATIVE,
    confidence: float = 0.6,
    call_wall: GEXWall | None = None,
    put_wall: GEXWall | None = None,
) -> GEXSetup:
    return GEXSetup(
        ticker="AAPL", as_of=AS_OF, spot_price=Decimal("195"), regime=regime,
        flip_point=None, nearest_call_wall=call_wall, nearest_put_wall=put_wall,
        target_level=Decimal("200"), candidate_direction=direction,
        setup_type="momentum" if direction != "none" else "none",
        structure_confidence=confidence, raw_gex_by_strike=[],
    )


# ---------------------------------------------------------------------------
# No exit — all clear
# ---------------------------------------------------------------------------


class TestNoExit:
    def test_no_signal_when_all_clear(self):
        pos = _position()
        result = DEFAULT_MONITOR.evaluate(
            pos,
            current_price=Decimal("195"),   # below target 200
            current_premium=Decimal("3.50"),  # +16.7% (not stopped)
            dte=14,
            as_of=AS_OF,
        )
        assert result is None

    def test_no_signal_at_dte_just_above_floor(self):
        pos = _position()
        result = DEFAULT_MONITOR.evaluate(
            pos,
            current_price=Decimal("195"),
            current_premium=Decimal("3.00"),
            dte=8,
            as_of=AS_OF,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Profit target
# ---------------------------------------------------------------------------


class TestProfitTarget:
    def test_fires_when_price_equals_target(self):
        pos = _position(target_level="200")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("200"), current_premium=Decimal("5.00"), dte=14, as_of=AS_OF
        )
        assert result is not None
        assert result.reason == ExitReason.PROFIT_TARGET

    def test_fires_when_price_above_target(self):
        pos = _position(target_level="200")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("201"), current_premium=Decimal("5.20"), dte=14, as_of=AS_OF
        )
        assert result.reason == ExitReason.PROFIT_TARGET

    def test_does_not_fire_below_target(self):
        # Below both the exact target and the 1.5% wall-proximity band
        # (threshold is 200 * (1 - 0.015) = 197.0)
        pos = _position(target_level="200")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("196.99"), current_premium=Decimal("4.80"), dte=14, as_of=AS_OF
        )
        assert result is None

    def test_signal_contains_correct_pnl_pct(self):
        pos = _position(entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("200"), current_premium=Decimal("4.50"), dte=14, as_of=AS_OF
        )
        assert result.pnl_pct == pytest.approx(0.50, rel=1e-4)  # +50%

    def test_signal_carries_position_id(self):
        pos = _position()
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("200"), current_premium=Decimal("5.00"), dte=14, as_of=AS_OF
        )
        assert result.position_id == "pos-001"

    def test_signal_as_of_uses_provided_timestamp(self):
        pos = _position()
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("200"), current_premium=Decimal("5.00"), dte=14, as_of=AS_OF
        )
        assert result.as_of == AS_OF


# ---------------------------------------------------------------------------
# Stop loss
# ---------------------------------------------------------------------------


class TestStopLoss:
    def test_fires_at_35_pct_loss_exactly(self):
        pos = _position(entry_premium="3.00")
        # current = 3.00 × (1 - 0.35) = 1.95
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("190"), current_premium=Decimal("1.95"), dte=14, as_of=AS_OF
        )
        assert result.reason == ExitReason.STOP_LOSS

    def test_fires_when_loss_exceeds_threshold(self):
        pos = _position(entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("188"), current_premium=Decimal("1.50"), dte=14, as_of=AS_OF
        )
        assert result.reason == ExitReason.STOP_LOSS

    def test_does_not_fire_below_threshold(self):
        pos = _position(entry_premium="3.00")
        # -34% → above the -35% threshold, should NOT stop
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("191"), current_premium=Decimal("1.98"), dte=14, as_of=AS_OF
        )
        assert result is None

    def test_stop_signal_pnl_pct_is_negative(self):
        pos = _position(entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("188"), current_premium=Decimal("1.50"), dte=14, as_of=AS_OF
        )
        assert result.pnl_pct < 0

    def test_custom_stop_loss_pct(self):
        monitor = ExitMonitor(stop_loss_pct=0.50)
        pos = _position(entry_premium="3.00")
        # -35% should NOT fire with 50% threshold
        result = monitor.evaluate(
            pos, current_price=Decimal("190"), current_premium=Decimal("1.95"), dte=14, as_of=AS_OF
        )
        assert result is None
        # -50% exactly should fire
        result = monitor.evaluate(
            pos, current_price=Decimal("185"), current_premium=Decimal("1.50"), dte=14, as_of=AS_OF
        )
        assert result.reason == ExitReason.STOP_LOSS


# ---------------------------------------------------------------------------
# DTE stop
# ---------------------------------------------------------------------------


class TestDTEStop:
    def test_fires_at_dte_floor(self):
        pos = _position()
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.00"), dte=7, as_of=AS_OF
        )
        assert result.reason == ExitReason.DTE_STOP

    def test_fires_below_dte_floor(self):
        pos = _position()
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("2.50"), dte=3, as_of=AS_OF
        )
        assert result.reason == ExitReason.DTE_STOP

    def test_does_not_fire_above_dte_floor(self):
        pos = _position()
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.00"), dte=8, as_of=AS_OF
        )
        assert result is None

    def test_custom_dte_floor(self):
        monitor = ExitMonitor(dte_floor=14)
        pos = _position()
        result = monitor.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.00"), dte=14, as_of=AS_OF
        )
        assert result.reason == ExitReason.DTE_STOP
        result = monitor.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.00"), dte=15, as_of=AS_OF
        )
        assert result is None


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------


class TestThesisInvalidation:
    def test_fires_when_regime_goes_mixed(self):
        # position holds a call; live setup now shows no structure at all
        pos = _position(target_level="200", entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.10"),
            dte=14, as_of=AS_OF, current_setup=_setup(direction="none", regime=GEXRegime.MIXED),
        )
        assert result is not None
        assert result.reason == ExitReason.THESIS_INVALIDATED

    def test_fires_when_direction_flips(self):
        # bought a call; live setup now favors puts
        pos = _position(target_level="200", entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.10"),
            dte=14, as_of=AS_OF, current_setup=_setup(direction="put"),
        )
        assert result is not None
        assert result.reason == ExitReason.THESIS_INVALIDATED

    def test_does_not_fire_when_direction_still_matches(self):
        pos = _position(target_level="200", entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.10"),
            dte=14, as_of=AS_OF, current_setup=_setup(direction="call"),
        )
        assert result is None

    def test_no_setup_provided_never_fires(self):
        # back-compat: omitting current_setup must not change existing behavior
        pos = _position(target_level="200", entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.10"),
            dte=14, as_of=AS_OF,
        )
        assert result is None

    def test_profit_target_takes_priority_over_thesis_invalidated(self):
        # price already at the wall — take the win even if the setup soured
        pos = _position(target_level="200", entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("200"), current_premium=Decimal("5.00"),
            dte=14, as_of=AS_OF, current_setup=_setup(direction="none", regime=GEXRegime.MIXED),
        )
        assert result.reason == ExitReason.PROFIT_TARGET

    def test_thesis_invalidated_takes_priority_over_stop_loss(self):
        # setup soured AND price already crashed past stop-loss — thesis wins
        # since it's evaluated first (checked in priority order)
        pos = _position(target_level="200", entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("1.50"),  # -50%
            dte=14, as_of=AS_OF, current_setup=_setup(direction="none", regime=GEXRegime.MIXED),
        )
        assert result.reason == ExitReason.THESIS_INVALIDATED


class TestThesisConfidenceDecay:
    # Held position is a "call" — entry snapshot has confidence 0.6 and a
    # call_wall at 2% distance. Default monitor: decay_pct=0.50 (floor 0.30),
    # wall_drift_pct=1.0 (ceiling = 2% * 2 = 4%).

    def test_no_entry_snapshot_never_fires_on_decay_alone(self):
        # No entry_gex_setup (reconciled/adopted position) — nothing to diff
        # against, so only the binary flip check (already covered above) applies.
        pos = _position(target_level="500", entry_gex_setup=None)
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.10"),
            dte=14, as_of=AS_OF,
            current_setup=_setup(direction="call", confidence=0.05),
        )
        assert result is None

    def test_fires_when_confidence_falls_below_decay_floor(self):
        entry_setup = _setup(direction="call", confidence=0.6, call_wall=_wall("0.02"))
        pos = _position(target_level="500", entry_gex_setup=entry_setup)
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.10"),
            dte=14, as_of=AS_OF,
            current_setup=_setup(direction="call", confidence=0.20, call_wall=_wall("0.02")),
        )
        assert result is not None
        assert result.reason == ExitReason.THESIS_INVALIDATED

    def test_does_not_fire_when_confidence_holds_above_floor(self):
        entry_setup = _setup(direction="call", confidence=0.6, call_wall=_wall("0.02"))
        pos = _position(target_level="500", entry_gex_setup=entry_setup)
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.10"),
            dte=14, as_of=AS_OF,
            current_setup=_setup(direction="call", confidence=0.45, call_wall=_wall("0.02")),
        )
        assert result is None

    def test_fires_when_held_side_wall_drifts_beyond_ceiling(self):
        entry_setup = _setup(direction="call", confidence=0.6, call_wall=_wall("0.02"))
        pos = _position(target_level="500", entry_gex_setup=entry_setup)
        # confidence unchanged (0.6, well above the 0.30 floor) but the call
        # wall retreated from 2% to 5% distance — beyond the 4% ceiling
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.10"),
            dte=14, as_of=AS_OF,
            current_setup=_setup(direction="call", confidence=0.6, call_wall=_wall("0.05")),
        )
        assert result is not None
        assert result.reason == ExitReason.THESIS_INVALIDATED

    def test_does_not_fire_when_wall_drift_within_tolerance(self):
        entry_setup = _setup(direction="call", confidence=0.6, call_wall=_wall("0.02"))
        pos = _position(target_level="500", entry_gex_setup=entry_setup)
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.10"),
            dte=14, as_of=AS_OF,
            current_setup=_setup(direction="call", confidence=0.6, call_wall=_wall("0.03")),
        )
        assert result is None

    def test_fires_when_held_side_wall_disappears(self):
        entry_setup = _setup(direction="call", confidence=0.6, call_wall=_wall("0.02"))
        pos = _position(target_level="500", entry_gex_setup=entry_setup)
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.10"),
            dte=14, as_of=AS_OF,
            current_setup=_setup(direction="call", confidence=0.6, call_wall=None),
        )
        assert result is not None
        assert result.reason == ExitReason.THESIS_INVALIDATED

    def test_no_drift_signal_when_entry_had_no_wall_either(self):
        # entry setup itself never had a call_wall (e.g. NEGATIVE-momentum
        # setup type) — nothing to compare drift against, so it's a no-op
        entry_setup = _setup(direction="call", confidence=0.6, call_wall=None)
        pos = _position(target_level="500", entry_gex_setup=entry_setup)
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.10"),
            dte=14, as_of=AS_OF,
            current_setup=_setup(direction="call", confidence=0.6, call_wall=None),
        )
        assert result is None

    def test_custom_decay_pct_and_wall_drift_pct(self):
        monitor = ExitMonitor(thesis_confidence_decay_pct=0.80, thesis_wall_drift_pct=0.10)
        entry_setup = _setup(direction="call", confidence=0.6, call_wall=_wall("0.02"))
        pos = _position(target_level="500", entry_gex_setup=entry_setup)
        # confidence 0.40 would pass the default 0.30 floor but fails the
        # stricter 0.80*0.6=0.48 floor here
        result = monitor.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.10"),
            dte=14, as_of=AS_OF,
            current_setup=_setup(direction="call", confidence=0.40, call_wall=_wall("0.02")),
        )
        assert result.reason == ExitReason.THESIS_INVALIDATED

    def test_profit_target_takes_priority_over_confidence_decay(self):
        entry_setup = _setup(direction="call", confidence=0.6, call_wall=_wall("0.02"))
        pos = _position(target_level="200", entry_gex_setup=entry_setup)
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("200"), current_premium=Decimal("5.00"),
            dte=14, as_of=AS_OF,
            current_setup=_setup(direction="call", confidence=0.05, call_wall=None),
        )
        assert result.reason == ExitReason.PROFIT_TARGET


class TestIVScaledThresholds:
    # DEFAULT_MONITOR: wall_proximity_pct=0.015, stop_loss_pct=0.35,
    # trailing_stop_giveback_pct=0.50, iv_scale_max_adjustment_pct=0.50 (class default).

    def test_high_iv_widens_profit_target_band_fires_earlier(self):
        # base threshold: 200*(1-0.015)=197.0 — 196 would NOT fire without IV.
        # high-IV (p=100) widened threshold: 200*(1-0.0225)=195.5 — 196 fires.
        pos = _position(target_level="200")
        no_context = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("196"), current_premium=Decimal("3.50"), dte=14, as_of=AS_OF,
        )
        assert no_context is None
        with_context = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("196"), current_premium=Decimal("3.50"), dte=14, as_of=AS_OF,
            context=_context("100", dte=14),
        )
        assert with_context.reason == ExitReason.PROFIT_TARGET

    def test_low_iv_narrows_profit_target_band_fires_later(self):
        # base threshold 197.0 would fire at 197.5; low-IV (p=0) narrowed
        # threshold 200*(1-0.0075)=198.5 — 197.5 no longer fires.
        pos = _position(target_level="200")
        no_context = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("197.5"), current_premium=Decimal("3.50"), dte=14, as_of=AS_OF,
        )
        assert no_context.reason == ExitReason.PROFIT_TARGET
        with_context = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("197.5"), current_premium=Decimal("3.50"), dte=14, as_of=AS_OF,
            context=_context("0", dte=14),
        )
        assert with_context is None

    def test_high_iv_widens_stop_loss_avoids_noise_stopout(self):
        # base stop_loss_pct=0.35 → -40% would fire without IV context.
        # high-IV (p=100) widened to 0.525 → -40% no longer fires.
        pos = _position(target_level="500", entry_premium="3.00")
        no_context = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("1.80"), dte=14, as_of=AS_OF,
        )
        assert no_context.reason == ExitReason.STOP_LOSS
        with_context = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("1.80"), dte=14, as_of=AS_OF,
            context=_context("100", dte=14),
        )
        assert with_context is None

    def test_low_iv_tightens_stop_loss_fires_sooner(self):
        # base stop_loss_pct=0.35 → -20% would NOT fire without IV context.
        # low-IV (p=0) tightened to 0.175 → -20% fires.
        pos = _position(target_level="500", entry_premium="3.00")
        no_context = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("2.40"), dte=14, as_of=AS_OF,
        )
        assert no_context is None
        with_context = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("2.40"), dte=14, as_of=AS_OF,
            context=_context("0", dte=14),
        )
        assert with_context.reason == ExitReason.STOP_LOSS

    def test_high_iv_narrows_trailing_giveback_locks_gains_sooner(self):
        # entry=4.00 peak=8.00 gain=4.00. Base floor=4+4*0.50=6.00 (6.50 stays
        # above it, no fire). High-IV (p=100) narrows giveback to 0.25 →
        # floor=4+4*0.75=7.00 — 6.50 now fires.
        pos = _position(target_level="500", entry_premium="4.00", peak_premium="8.00")
        no_context = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("6.50"), dte=14, as_of=AS_OF,
        )
        assert no_context is None
        with_context = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("6.50"), dte=14, as_of=AS_OF,
            context=_context("100", dte=14),
        )
        assert with_context.reason == ExitReason.TRAILING_STOP

    def test_low_iv_widens_trailing_giveback_lets_it_run(self):
        # Base floor 6.00 fires at 5.90. Low-IV (p=0) widens giveback to
        # 0.75 → floor=4+4*0.25=5.00 — 5.90 no longer fires.
        pos = _position(target_level="500", entry_premium="4.00", peak_premium="8.00")
        no_context = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("5.90"), dte=14, as_of=AS_OF,
        )
        assert no_context.reason == ExitReason.TRAILING_STOP
        with_context = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("5.90"), dte=14, as_of=AS_OF,
            context=_context("0", dte=14),
        )
        assert with_context is None

    def test_context_with_no_iv_data_at_dte_behaves_like_no_context(self):
        pos = _position(target_level="200")
        empty_context = ExitContext()  # no interpolated_iv entries at all
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("196"), current_premium=Decimal("3.50"), dte=14, as_of=AS_OF,
            context=empty_context,
        )
        assert result is None

    def test_zero_max_adjustment_disables_scaling_even_at_extreme_iv(self):
        monitor = ExitMonitor(iv_scale_max_adjustment_pct=0.0)
        pos = _position(target_level="200")
        result = monitor.evaluate(
            pos, current_price=Decimal("196"), current_premium=Decimal("3.50"), dte=14, as_of=AS_OF,
            context=_context("100", dte=14),
        )
        assert result is None


class TestMomentumConfirmation:
    # Held position is a "call". Base wall_proximity_pct=0.015, target=200 →
    # base threshold 200*(1-0.015)=197.0. Default momentum_wall_adjustment_pct=0.50.

    def test_confirming_momentum_narrows_band_delays_exit(self):
        # RSI=60 (>=55 confirm) + MACD histogram positive → confirm → band
        # narrows to 0.0075 → threshold 198.5. 197.5 fires at base, not here.
        pos = _position(target_level="200")
        base = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("197.5"), current_premium=Decimal("3.50"),
            dte=14, as_of=AS_OF,
        )
        assert base.reason == ExitReason.PROFIT_TARGET
        with_momentum = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("197.5"), current_premium=Decimal("3.50"),
            dte=14, as_of=AS_OF, context=_momentum_context(rsi="60", macd_histogram="0.5"),
        )
        assert with_momentum is None

    def test_diverging_momentum_widens_band_fires_earlier(self):
        # RSI=40 (<=45 diverge) → diverge → band widens to 0.0225 →
        # threshold 195.5. 196 does NOT fire at base, fires here.
        pos = _position(target_level="200")
        base = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("196"), current_premium=Decimal("3.50"),
            dte=14, as_of=AS_OF,
        )
        assert base is None
        with_momentum = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("196"), current_premium=Decimal("3.50"),
            dte=14, as_of=AS_OF, context=_momentum_context(rsi="40", macd_histogram="0.5"),
        )
        assert with_momentum.reason == ExitReason.PROFIT_TARGET

    def test_diverging_macd_alone_widens_band(self):
        # RSI neutral (50) but MACD histogram negative — still diverge for a call
        pos = _position(target_level="200")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("196"), current_premium=Decimal("3.50"),
            dte=14, as_of=AS_OF, context=_momentum_context(rsi="50", macd_histogram="-0.1"),
        )
        assert result.reason == ExitReason.PROFIT_TARGET

    def test_neutral_signals_leave_band_unchanged(self):
        pos = _position(target_level="200")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("197.5"), current_premium=Decimal("3.50"),
            dte=14, as_of=AS_OF, context=_momentum_context(rsi="50", macd_histogram="0.1"),
        )
        # base threshold 197.0 → 197.5 fires regardless (unchanged band)
        assert result.reason == ExitReason.PROFIT_TARGET

    def test_missing_macd_treated_as_neutral(self):
        pos = _position(target_level="200")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("196"), current_premium=Decimal("3.50"),
            dte=14, as_of=AS_OF, context=_momentum_context(rsi="60", macd_histogram=None),
        )
        assert result is None  # below base threshold 197.0, no momentum adjustment applied

    def test_put_direction_mirrors_thresholds(self):
        from datetime import date as _date

        put_contract = OptionContract(
            ticker="AAPL", expiry=_date(2026, 7, 25), strike=Decimal("190"),
            type="put", bid=Decimal("2.90"), ask=Decimal("3.10"),
            open_interest=9000, volume=4500,
        )
        pos = Position(
            position_id="pos-put", ticker="AAPL", contract=put_contract,
            entry_premium=Decimal("3.00"), target_level=Decimal("190"),
            opened_at=datetime(2026, 6, 28, 10, 0, tzinfo=timezone.utc),
        )
        # base threshold: 190*(1+0.015)=192.85 — 192.5 fires at base (put: price<=threshold)
        base = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("192.5"), current_premium=Decimal("3.50"),
            dte=14, as_of=AS_OF,
        )
        assert base.reason == ExitReason.PROFIT_TARGET
        # confirming bearish momentum (RSI<=45, MACD<0) narrows the band —
        # threshold drops to 190*(1+0.0075)=191.425, 192.5 no longer fires
        with_momentum = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("192.5"), current_premium=Decimal("3.50"),
            dte=14, as_of=AS_OF, context=_momentum_context(rsi="40", macd_histogram="-0.5"),
        )
        assert with_momentum is None

    def test_zero_adjustment_disables_momentum_effect(self):
        monitor = ExitMonitor(momentum_wall_adjustment_pct=0.0)
        pos = _position(target_level="200")
        result = monitor.evaluate(
            pos, current_price=Decimal("196"), current_premium=Decimal("3.50"),
            dte=14, as_of=AS_OF, context=_momentum_context(rsi="40", macd_histogram="-0.5"),
        )
        assert result is None


class TestGammaWallStructureAwareness:
    # Held position is a "call", entry target_level=200. current_setup's
    # nearest_call_wall reflects the *live* wall re-derived each tick.

    def test_uses_live_wall_instead_of_stale_entry_target(self):
        # entry target 200 already reached (spot=201), but live wall has
        # moved out to 210 (e.g. GEX structure shifted) — live wall wins,
        # so profit target does NOT fire yet at 201.
        pos = _position(target_level="200")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("201"), current_premium=Decimal("5.00"),
            dte=14, as_of=AS_OF,
            current_setup=_setup(direction="call", call_wall=_wall("0.02", side="call_wall", strike="210")),
        )
        assert result is None

    def test_fires_once_price_reaches_the_advanced_live_wall(self):
        pos = _position(target_level="200")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("210"), current_premium=Decimal("6.00"),
            dte=14, as_of=AS_OF,
            current_setup=_setup(direction="call", call_wall=_wall("0.001", side="call_wall", strike="210")),
        )
        assert result.reason == ExitReason.PROFIT_TARGET

    def test_falls_back_to_entry_target_when_no_live_wall_available(self):
        # current_setup present but its call_wall is None (e.g. spot ran past
        # every strike in the chain) — fall back to the entry snapshot.
        pos = _position(target_level="200")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("200"), current_premium=Decimal("5.00"),
            dte=14, as_of=AS_OF,
            current_setup=_setup(direction="call", call_wall=None),
        )
        assert result.reason == ExitReason.PROFIT_TARGET

    def test_falls_back_to_entry_target_when_no_current_setup(self):
        # no live context at all (stale/no cache) — same as pre-2d behavior.
        pos = _position(target_level="200")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("200"), current_premium=Decimal("5.00"),
            dte=14, as_of=AS_OF,
        )
        assert result.reason == ExitReason.PROFIT_TARGET

    def test_put_direction_uses_live_put_wall(self):
        put_contract = OptionContract(
            ticker="AAPL", expiry=date(2026, 7, 25), strike=Decimal("190"),
            type="put", bid=Decimal("2.90"), ask=Decimal("3.10"),
            open_interest=9000, volume=4500,
        )
        pos = Position(
            position_id="pos-put", ticker="AAPL", contract=put_contract,
            entry_premium=Decimal("3.00"), target_level=Decimal("190"),
            opened_at=datetime(2026, 6, 28, 10, 0, tzinfo=timezone.utc),
        )
        # entry target 190 already reached (spot=189), but live put wall has
        # moved out to 180 — live wall wins, no fire yet at 189
        live_put_wall = _wall("0.02", side="put_wall", strike="180")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("189"), current_premium=Decimal("5.00"),
            dte=14, as_of=AS_OF,
            current_setup=_setup(direction="put", put_wall=live_put_wall),
        )
        assert result is None

    def test_no_target_level_never_uses_live_wall_either(self):
        # reconciled/adopted positions (target_level=None) stay exempt from
        # the profit-target gate entirely, live wall or not.
        contract = _contract()
        pos = Position(
            position_id="pos-recon", ticker="AAPL", contract=contract,
            entry_premium=Decimal("3.00"), target_level=None,
            opened_at=datetime(2026, 6, 28, 10, 0, tzinfo=timezone.utc),
        )
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("210"), current_premium=Decimal("6.00"),
            dte=14, as_of=AS_OF,
            current_setup=_setup(direction="call", call_wall=_wall("0.001", strike="210")),
        )
        assert result is None


class TestEarningsGap:
    # Default monitor: earnings_buffer_days=2, stop_loss_pct=0.35.

    def test_fires_when_profitable_and_earnings_within_buffer(self):
        pos = _position(target_level="500", entry_premium="3.00")  # target far — no profit_target
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.50"),  # +16.7%
            dte=14, as_of=AS_OF, days_to_earnings=1,
        )
        assert result is not None
        assert result.reason == ExitReason.EARNINGS_GAP

    def test_does_not_fire_when_profitable_but_earnings_outside_buffer(self):
        pos = _position(target_level="500", entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.50"),
            dte=14, as_of=AS_OF, days_to_earnings=5,
        )
        assert result is None

    def test_does_not_fire_when_at_a_loss_near_earnings(self):
        # -10% loss, within buffer — holds instead of forcing a loss sale
        pos = _position(target_level="500", entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("2.70"),
            dte=14, as_of=AS_OF, days_to_earnings=1,
        )
        assert result is None

    def test_no_days_to_earnings_never_fires(self):
        pos = _position(target_level="500", entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.50"),
            dte=14, as_of=AS_OF,
        )
        assert result is None

    def test_suspends_iv_widened_stop_loss_when_at_a_loss_near_earnings(self):
        # High IV would normally widen stop_loss_pct to 0.525 (see
        # TestIVScaledThresholds), masking a -40% loss. Near earnings the
        # widening is suspended — capped back to the static 0.35 — so the
        # loss still stops out instead of being held open by elevated IV.
        pos = _position(target_level="500", entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("1.80"),  # -40%
            dte=14, as_of=AS_OF, context=_context("100", dte=14), days_to_earnings=1,
        )
        assert result.reason == ExitReason.STOP_LOSS

    def test_profit_target_takes_priority_over_earnings_gap(self):
        pos = _position(target_level="200", entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("200"), current_premium=Decimal("5.00"),
            dte=14, as_of=AS_OF, days_to_earnings=1,
        )
        assert result.reason == ExitReason.PROFIT_TARGET

    def test_earnings_gap_takes_priority_over_thesis_invalidated(self):
        pos = _position(target_level="500", entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.50"),
            dte=14, as_of=AS_OF, days_to_earnings=1,
            current_setup=_setup(direction="none", regime=GEXRegime.MIXED),
        )
        assert result.reason == ExitReason.EARNINGS_GAP


class TestLiquidityAwareness:
    def test_wide_spread_widens_profit_target_band_fires_earlier(self):
        # base threshold 197.0 — 196 does not fire without spread signal
        pos = _position(target_level="200")
        base = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("196"), current_premium=Decimal("3.50"), dte=14, as_of=AS_OF,
        )
        assert base is None
        with_spread = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("196"), current_premium=Decimal("3.50"), dte=14, as_of=AS_OF,
            spread_pct=Decimal("0.20"),  # above default 0.15 threshold
        )
        assert with_spread.reason == ExitReason.PROFIT_TARGET

    def test_narrow_spread_leaves_band_unchanged(self):
        pos = _position(target_level="200")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("196"), current_premium=Decimal("3.50"), dte=14, as_of=AS_OF,
            spread_pct=Decimal("0.05"),  # below default 0.15 threshold
        )
        assert result is None

    def test_zero_adjustment_disables_liquidity_effect(self):
        monitor = ExitMonitor(liquidity_wall_adjustment_pct=0.0)
        pos = _position(target_level="200")
        result = monitor.evaluate(
            pos, current_price=Decimal("196"), current_premium=Decimal("3.50"), dte=14, as_of=AS_OF,
            spread_pct=Decimal("0.50"),
        )
        assert result is None


class TestTrailingStop:
    # Default monitor: activation 0.30 (30% gain to arm), giveback 0.50 (exit
    # after losing half the peak gain). entry=4.00 → activation threshold
    # = 5.20; peak=8.00 → gain_at_peak=4.00 → giveback_floor = 4.00 + 4.00*0.50 = 6.00

    def test_does_not_fire_without_peak_data(self):
        # position never had its peak tracked (e.g. first tick after entry)
        pos = _position(target_level="500", entry_premium="4.00", peak_premium=None)
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("1.00"), dte=14, as_of=AS_OF,
        )
        # stop_loss fires instead (peak-based trailing stop simply isn't armed)
        assert result.reason == ExitReason.STOP_LOSS

    def test_does_not_fire_when_never_reached_activation_threshold(self):
        # peak only +12.5% over entry — below the 30% activation bar
        pos = _position(target_level="500", entry_premium="4.00", peak_premium="4.50")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("1.00"), dte=14, as_of=AS_OF,
        )
        assert result.reason == ExitReason.STOP_LOSS  # falls through to the plain stop-loss

    def test_fires_once_giveback_threshold_breached(self):
        pos = _position(target_level="500", entry_premium="4.00", peak_premium="8.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("5.90"), dte=14, as_of=AS_OF,
        )
        assert result is not None
        assert result.reason == ExitReason.TRAILING_STOP

    def test_does_not_fire_while_still_holding_most_of_the_gain(self):
        pos = _position(target_level="500", entry_premium="4.00", peak_premium="8.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("6.10"), dte=14, as_of=AS_OF,
        )
        assert result is None

    def test_fires_exactly_at_giveback_floor(self):
        pos = _position(target_level="500", entry_premium="4.00", peak_premium="8.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("6.00"), dte=14, as_of=AS_OF,
        )
        assert result.reason == ExitReason.TRAILING_STOP

    def test_thesis_invalidated_takes_priority_over_trailing_stop(self):
        pos = _position(target_level="500", entry_premium="4.00", peak_premium="8.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("5.90"), dte=14, as_of=AS_OF,
            current_setup=_setup(direction="none", regime=GEXRegime.MIXED),
        )
        assert result.reason == ExitReason.THESIS_INVALIDATED

    def test_trailing_stop_takes_priority_over_stop_loss(self):
        # Both conditions independently true: giveback_floor breached (6.00)
        # AND price-based stop-loss breached (-35% of 4.00 = 2.60) — trailing
        # stop is checked first and wins.
        pos = _position(target_level="500", entry_premium="4.00", peak_premium="8.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("2.50"), dte=14, as_of=AS_OF,
        )
        assert result.reason == ExitReason.TRAILING_STOP

    def test_profit_target_takes_priority_over_trailing_stop(self):
        pos = _position(target_level="200", entry_premium="4.00", peak_premium="8.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("200"), current_premium=Decimal("5.90"), dte=14, as_of=AS_OF,
        )
        assert result.reason == ExitReason.PROFIT_TARGET


class TestPriority:
    def test_profit_target_over_stop_loss(self):
        # Both triggered: price at wall + premium cratered (somehow — edge case)
        pos = _position(target_level="200", entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos,
            current_price=Decimal("200"),   # at target → profit_target
            current_premium=Decimal("1.50"),  # -50% → stop_loss would also fire
            dte=14,
            as_of=AS_OF,
        )
        assert result.reason == ExitReason.PROFIT_TARGET

    def test_profit_target_over_dte_stop(self):
        pos = _position(target_level="200")
        result = DEFAULT_MONITOR.evaluate(
            pos,
            current_price=Decimal("200"),  # at target
            current_premium=Decimal("5.00"),
            dte=3,                          # also below floor
            as_of=AS_OF,
        )
        assert result.reason == ExitReason.PROFIT_TARGET

    def test_stop_loss_over_dte_stop(self):
        pos = _position(entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos,
            current_price=Decimal("190"),   # below target, no profit_target
            current_premium=Decimal("1.50"),  # -50% → stop_loss
            dte=5,                           # also below floor
            as_of=AS_OF,
        )
        assert result.reason == ExitReason.STOP_LOSS

    def test_only_first_reason_returned(self):
        pos = _position(target_level="200", entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos,
            current_price=Decimal("200"),
            current_premium=Decimal("1.50"),
            dte=3,
            as_of=AS_OF,
        )
        # All three could fire; only PROFIT_TARGET returned
        assert result.reason == ExitReason.PROFIT_TARGET


# ---------------------------------------------------------------------------
# ExitSignal fields
# ---------------------------------------------------------------------------


class TestExitSignalFields:
    def test_entry_and_current_premium_preserved(self):
        pos = _position(entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("200"), current_premium=Decimal("5.00"), dte=14, as_of=AS_OF
        )
        assert result.entry_premium == Decimal("3.00")
        assert result.current_premium == Decimal("5.00")

    def test_dte_remaining_preserved(self):
        pos = _position()
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("200"), current_premium=Decimal("5.00"), dte=12, as_of=AS_OF
        )
        assert result.dte_remaining == 12

    def test_ticker_preserved(self):
        pos = _position()
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("200"), current_premium=Decimal("5.00"), dte=14, as_of=AS_OF
        )
        assert result.ticker == "AAPL"
