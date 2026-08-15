from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from .schemas import ExitContext, ExitReason, ExitSignal, Position

if TYPE_CHECKING:
    from trader.gex.schemas import GEXSetup


class ExitMonitor:
    """
    Evaluates an open position on each monitoring tick and returns an ExitSignal
    if any exit condition is triggered.

    Priority order (first match wins):
      1. Profit target      — underlying price within wall_proximity_pct of the GEX gamma wall
      2. Thesis invalidated — the live GEX setup no longer supports the direction
                               this position was bought for (regime flipped or
                               went mixed), OR the structure has decayed
                               meaningfully from its entry-time snapshot even
                               without a full flip: live structure_confidence
                               fell below thesis_confidence_decay_pct of entry
                               confidence, or the held-side wall's distance
                               from spot grew by more than thesis_wall_drift_pct
                               vs entry (or the wall disappeared). Exits before
                               price-based stop-loss would otherwise have to
                               absorb the full decay.
      3. Trailing stop       — position ran up ≥ trailing_stop_activation_pct over
                               entry at some point (position.peak_premium), then gave
                               back ≥ trailing_stop_giveback_pct of that peak gain.
                               Catches the case thesis invalidation doesn't: the GEX
                               setup never structurally flipped, price just ran up and
                               reversed without ever reaching the wall.
      3.5 Earnings gap        — days_to_earnings ≤ earnings_buffer_days AND the
                               position is currently profitable: lock in gains
                               ahead of a binary event instead of risking an
                               IV-crush/gap through it. If unprofitable near
                               earnings, holds instead (a loss forced out right
                               before an event it might recover) but suspends
                               any IV-driven widening of the stop-loss (below)
                               so elevated pre-earnings IV can't hold a loser
                               open past what the static config intends.
      4. Stop loss           — option premium dropped ≥ stop_loss_pct from entry
      5. DTE stop             — dte_remaining ≤ dte_floor (avoid final-week decay)

    wall_proximity_pct: how close spot must get to the gamma wall to trigger profit
    exit. Default 1.5% — avoids requiring an exact wall touch, which rarely happens.
    Direction-aware: calls exit when spot ≥ wall × (1 - pct); puts exit when
    spot ≤ wall × (1 + pct).

    trailing_stop_activation_pct: minimum gain over entry (measured at
    position.peak_premium, the highest premium observed since entry — tracked and
    persisted by the caller, not this class) required to arm the trailing stop.
    trailing_stop_giveback_pct: fraction of the gain-above-entry at peak that may
    be given back before exiting. E.g. entry=4.00, peak=8.00 (gain=4.00), giveback
    0.50 → exits once premium falls to 4.00 + 4.00*(1-0.50) = 6.00.

    iv_scale_max_adjustment_pct: when context carries an IV percentile for the
    position's DTE, wall_proximity_pct/stop_loss_pct/trailing_stop_giveback_pct
    are scaled by up to ±this fraction around IV percentile 50 (neutral) before
    any gate is evaluated — high IV widens the stop and takes profit sooner
    (vol-crush risk into the target), low IV tightens the stop and lets the
    trailing stop run further (a low-noise move is more likely "real"). 0
    disables scaling. No IV data available → multiplier is exactly 1 (identical
    to the static thresholds above).

    momentum_wall_adjustment_pct: further adjusts the (already IV-scaled) profit-
    target band using the position's DTE-agnostic RSI/MACD from context. If both
    agree with continuation in the trade's direction (RSI past
    momentum_rsi_confirm_threshold, MACD histogram signed the same way), the band
    narrows by this fraction — let it ride closer to/through the wall instead of
    exiting immediately. If either warns of reversal (RSI past
    momentum_rsi_diverge_threshold on the wrong side, or histogram signed against
    the trade), the band widens by this fraction — take the win now. Needs both
    RSI and MACD to have a value; missing either is treated as neutral (no
    adjustment). Only affects the profit-target gate.

    earnings_buffer_days: see gate 3.5 above. days_to_earnings and spread_pct
    (see below) are plain arguments like current_setup — any I/O to obtain
    them (RH get_earnings_calendar / get_option_price_book) happens in the
    caller.

    liquidity_spread_widen_threshold_pct / liquidity_wall_adjustment_pct: when
    spread_pct (bid-ask spread as a fraction of mid, from the caller) exceeds
    the threshold, the profit-target band widens by the adjustment fraction —
    deteriorating liquidity means waiting for an exact wall touch risks not
    getting filled at a reasonable price at all, so take the win sooner.
    Stacks with the IV/momentum adjustments above.

    Fully synchronous; no I/O. Thesis invalidation and the trailing-stop peak both
    arrive as plain arguments — any cache/state lookup happens in the caller.

    dynamic_exits_enabled: master switch for every adjustment described above
    (IV scaling, momentum confirmation, gamma-wall structure resolution,
    thesis-confidence decay, earnings gap, liquidity awareness). False
    reproduces the original static-threshold behavior exactly: entry-snapshotted
    target_level, binary thesis direction-flip only, no IV/momentum/earnings/
    liquidity adjustments. True by default here since callers construct this
    directly (tests, backtest); the live default lives in LiveConfig and is
    False until explicitly enabled.
    """

    def __init__(
        self,
        stop_loss_pct: float = 0.35,
        dte_floor: int = 7,
        wall_proximity_pct: float = 0.015,
        trailing_stop_activation_pct: float = 0.30,
        trailing_stop_giveback_pct: float = 0.50,
        thesis_confidence_decay_pct: float = 0.50,
        thesis_wall_drift_pct: float = 1.0,
        iv_scale_max_adjustment_pct: float = 0.50,
        momentum_wall_adjustment_pct: float = 0.50,
        momentum_rsi_confirm_threshold: float = 55.0,
        momentum_rsi_diverge_threshold: float = 45.0,
        earnings_buffer_days: int = 2,
        liquidity_spread_widen_threshold_pct: float = 0.15,
        liquidity_wall_adjustment_pct: float = 0.50,
        dynamic_exits_enabled: bool = True,
    ) -> None:
        self.dynamic_exits_enabled = dynamic_exits_enabled
        self.stop_loss_pct = stop_loss_pct
        self.dte_floor = dte_floor
        self.wall_proximity_pct = Decimal(str(wall_proximity_pct))
        self.trailing_stop_activation_pct = trailing_stop_activation_pct
        self.trailing_stop_giveback_pct = trailing_stop_giveback_pct
        self.iv_scale_max_adjustment_pct = iv_scale_max_adjustment_pct
        self.momentum_wall_adjustment_pct = momentum_wall_adjustment_pct
        self.momentum_rsi_confirm_threshold = momentum_rsi_confirm_threshold
        self.momentum_rsi_diverge_threshold = momentum_rsi_diverge_threshold
        self.earnings_buffer_days = earnings_buffer_days
        self.liquidity_spread_widen_threshold_pct = liquidity_spread_widen_threshold_pct
        self.liquidity_wall_adjustment_pct = liquidity_wall_adjustment_pct
        # Thesis-confidence decay (TODO.md:83-87): exit before a full
        # direction flip if the structure that justified the trade has
        # already eroded meaningfully from what it looked like at entry.
        self.thesis_confidence_decay_pct = thesis_confidence_decay_pct  # exit once live
            # structure_confidence falls below this fraction of entry confidence
        self.thesis_wall_drift_pct = thesis_wall_drift_pct  # exit once the held-side
            # wall's distance from spot grows by more than this fraction vs entry

    def evaluate(
        self,
        position: Position,
        current_price: Decimal,    # current underlying price
        current_premium: Decimal,  # current option mid (per share)
        dte: int,
        as_of: datetime | None = None,
        current_setup: "GEXSetup | None" = None,
        context: ExitContext | None = None,
        days_to_earnings: int | None = None,
        spread_pct: Decimal | None = None,
    ) -> ExitSignal | None:
        reason = self._first_triggered(
            position, current_price, current_premium, dte, current_setup, context,
            days_to_earnings, spread_pct,
        )
        if reason is None:
            return None

        pnl_pct = float(
            (current_premium - position.entry_premium) / position.entry_premium
        )

        return ExitSignal(
            position_id=position.position_id,
            ticker=position.ticker,
            contract=position.contract,
            reason=reason,
            current_premium=current_premium,
            entry_premium=position.entry_premium,
            pnl_pct=pnl_pct,
            dte_remaining=dte,
            as_of=as_of or datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _first_triggered(
        self,
        position: Position,
        current_price: Decimal,
        current_premium: Decimal,
        dte: int,
        current_setup: "GEXSetup | None" = None,
        context: ExitContext | None = None,
        days_to_earnings: int | None = None,
        spread_pct: Decimal | None = None,
    ) -> ExitReason | None:
        pnl_ratio = (current_premium - position.entry_premium) / position.entry_premium

        near_earnings = False
        effective_wall_proximity_pct = self.wall_proximity_pct
        effective_stop_loss_pct = Decimal(str(self.stop_loss_pct))
        effective_giveback_pct = Decimal(str(self.trailing_stop_giveback_pct))

        if self.dynamic_exits_enabled:
            iv_percentile = context.iv_percentile_at(dte) if context is not None else None
            # Widening multiplier for wall_proximity_pct/stop_loss_pct (high IV → give
            # more room / take profit sooner); narrowing for trailing_stop_giveback_pct
            # (high IV → lock gains in faster). Both collapse to exactly 1 with no IV
            # data, so behavior is unchanged when context/IV isn't available.
            widen = self._iv_multiplier(iv_percentile, invert=False)
            narrow = self._iv_multiplier(iv_percentile, invert=True)
            effective_wall_proximity_pct = effective_wall_proximity_pct * widen
            effective_stop_loss_pct = effective_stop_loss_pct * widen
            effective_giveback_pct = effective_giveback_pct * narrow

            momentum = self._momentum_signal(position, context)
            momentum_adj = Decimal(str(self.momentum_wall_adjustment_pct))
            if momentum == "confirm":
                effective_wall_proximity_pct *= 1 - momentum_adj  # narrower band — let it ride
            elif momentum == "diverge":
                effective_wall_proximity_pct *= 1 + momentum_adj  # wider band — take the win now

            if (
                spread_pct is not None
                and spread_pct > Decimal(str(self.liquidity_spread_widen_threshold_pct))
            ):
                # deteriorating liquidity — waiting for an exact wall touch risks
                # not getting filled at a reasonable price at all
                effective_wall_proximity_pct *= 1 + Decimal(str(self.liquidity_wall_adjustment_pct))

            near_earnings = (
                days_to_earnings is not None and days_to_earnings <= self.earnings_buffer_days
            )
            if near_earnings and pnl_ratio <= 0:
                # elevated pre-earnings IV shouldn't be allowed to widen the stop
                # further than the static config intends — don't hold a loser
                # open waiting for a bigger loss just because IV widened it
                effective_stop_loss_pct = min(effective_stop_loss_pct, Decimal(str(self.stop_loss_pct)))

        if position.target_level is not None:
            is_call = position.contract.type == "call"
            target = (
                self._resolve_live_target(position, current_setup, is_call)
                if self.dynamic_exits_enabled
                else position.target_level
            )
            if is_call and current_price >= target * (1 - effective_wall_proximity_pct):
                return ExitReason.PROFIT_TARGET
            if not is_call and current_price <= target * (1 + effective_wall_proximity_pct):
                return ExitReason.PROFIT_TARGET

        if near_earnings and pnl_ratio > 0:
            return ExitReason.EARNINGS_GAP

        if current_setup is not None:
            # The setup this position was bought for is gone: regime went mixed
            # (candidate_direction "none") or flipped to the opposite side of
            # what we're holding. Either way the original reason to hold no
            # longer applies — exit before a price-based stop has to catch it.
            if current_setup.candidate_direction != position.contract.type:
                return ExitReason.THESIS_INVALIDATED
            if self.dynamic_exits_enabled and self._thesis_confidence_decayed(position, current_setup):
                return ExitReason.THESIS_INVALIDATED

        if position.peak_premium is not None:
            activation = position.entry_premium * Decimal(str(1 + self.trailing_stop_activation_pct))
            gain_at_peak = position.peak_premium - position.entry_premium
            if position.peak_premium >= activation and gain_at_peak > 0:
                giveback_floor = position.entry_premium + gain_at_peak * (
                    1 - effective_giveback_pct
                )
                if current_premium <= giveback_floor:
                    return ExitReason.TRAILING_STOP

        if pnl_ratio <= -effective_stop_loss_pct:
            return ExitReason.STOP_LOSS

        if dte <= self.dte_floor:
            return ExitReason.DTE_STOP

        return None

    def _iv_multiplier(self, iv_percentile: Decimal | None, *, invert: bool) -> Decimal:
        """1 + s*max_adj (or 1 - s*max_adj when inverted), where s is IV
        percentile rescaled from [0,100] to [-1,1] around the neutral midpoint
        50. No IV data → exactly 1 (no-op)."""
        if iv_percentile is None:
            return Decimal("1")
        p = max(Decimal("0"), min(Decimal("100"), iv_percentile))
        s = (p - 50) / Decimal("50")
        adj = Decimal(str(self.iv_scale_max_adjustment_pct))
        delta = -s * adj if invert else s * adj
        return 1 + delta

    def _resolve_live_target(
        self, position: Position, current_setup: "GEXSetup | None", is_call: bool
    ) -> Decimal:
        """Prefer the live-resolved wall over the frozen entry-time snapshot
        (position.target_level) as gate 1's price target. current_setup is
        re-derived from live spot_gex each tick relative to *current* spot
        (see ExitLoop._current_gex_setup / StandardPolicy._current_gex_setup),
        so nearest_call_wall/nearest_put_wall already only consider strikes on
        the far side of current spot — if the entry wall gets breached, the
        next tick's live wall is automatically the next one out, with no
        separate breach-detection needed. Falls back to the entry snapshot
        when no live wall is available (stale/missing cache, or the wall
        disappeared), same as gate 1's prior behavior."""
        if current_setup is not None:
            live_wall = current_setup.nearest_call_wall if is_call else current_setup.nearest_put_wall
            if live_wall is not None:
                return live_wall.strike
        return position.target_level

    def _momentum_signal(self, position: Position, context: ExitContext | None) -> str:
        """"confirm" if RSI+MACD both agree with continuation in the trade's
        direction, "diverge" if either warns of reversal, else "neutral".
        Needs both indicators present — a single stale/missing signal isn't
        enough to act on, so it's treated as neutral rather than guessing."""
        if context is None:
            return "neutral"
        rsi = context.rsi_latest()
        macd_hist = context.macd_histogram_latest()
        if rsi is None or macd_hist is None:
            return "neutral"

        confirm_t = Decimal(str(self.momentum_rsi_confirm_threshold))
        diverge_t = Decimal(str(self.momentum_rsi_diverge_threshold))
        is_call = position.contract.type == "call"

        if is_call:
            if rsi >= confirm_t and macd_hist > 0:
                return "confirm"
            if rsi <= diverge_t or macd_hist < 0:
                return "diverge"
        else:
            if rsi <= (100 - confirm_t) and macd_hist < 0:
                return "confirm"
            if rsi >= (100 - diverge_t) or macd_hist > 0:
                return "diverge"
        return "neutral"

    def _thesis_confidence_decayed(
        self, position: Position, current_setup: "GEXSetup"
    ) -> bool:
        """True if the live structure has eroded meaningfully from its
        entry-time snapshot, even though candidate_direction hasn't flipped
        outright. No entry snapshot (reconciled/adopted positions, same as
        target_level=None) means there's nothing to diff against — fall back
        to the binary flip check above only."""
        entry_setup = position.entry_gex_setup
        if entry_setup is None or entry_setup.structure_confidence <= 0:
            return False

        decay_floor = entry_setup.structure_confidence * self.thesis_confidence_decay_pct
        if current_setup.structure_confidence < decay_floor:
            return True

        is_call = position.contract.type == "call"
        entry_wall = entry_setup.nearest_call_wall if is_call else entry_setup.nearest_put_wall
        live_wall = current_setup.nearest_call_wall if is_call else current_setup.nearest_put_wall
        if entry_wall is None:
            return False  # nothing to compare drift against
        if live_wall is None:
            return True  # the wall that supported this trade is gone
        drift_ceiling = entry_wall.distance_pct * Decimal(str(1 + self.thesis_wall_drift_pct))
        return live_wall.distance_pct > drift_ceiling
