from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from .schemas import ExitReason, ExitSignal, Position

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
                               went mixed) — exits before price-based stop-loss
                               would otherwise have to absorb the full decay
      3. Trailing stop       — position ran up ≥ trailing_stop_activation_pct over
                               entry at some point (position.peak_premium), then gave
                               back ≥ trailing_stop_giveback_pct of that peak gain.
                               Catches the case thesis invalidation doesn't: the GEX
                               setup never structurally flipped, price just ran up and
                               reversed without ever reaching the wall.
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

    Fully synchronous; no I/O. Thesis invalidation and the trailing-stop peak both
    arrive as plain arguments — any cache/state lookup happens in the caller.
    """

    def __init__(
        self,
        stop_loss_pct: float = 0.35,
        dte_floor: int = 7,
        wall_proximity_pct: float = 0.015,
        trailing_stop_activation_pct: float = 0.30,
        trailing_stop_giveback_pct: float = 0.50,
    ) -> None:
        self.stop_loss_pct = stop_loss_pct
        self.dte_floor = dte_floor
        self.wall_proximity_pct = Decimal(str(wall_proximity_pct))
        self.trailing_stop_activation_pct = trailing_stop_activation_pct
        self.trailing_stop_giveback_pct = trailing_stop_giveback_pct

    def evaluate(
        self,
        position: Position,
        current_price: Decimal,    # current underlying price
        current_premium: Decimal,  # current option mid (per share)
        dte: int,
        as_of: datetime | None = None,
        current_setup: "GEXSetup | None" = None,
    ) -> ExitSignal | None:
        reason = self._first_triggered(position, current_price, current_premium, dte, current_setup)
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
    ) -> ExitReason | None:
        if position.target_level is not None:
            target = position.target_level
            is_call = position.contract.type == "call"
            if is_call and current_price >= target * (1 - self.wall_proximity_pct):
                return ExitReason.PROFIT_TARGET
            if not is_call and current_price <= target * (1 + self.wall_proximity_pct):
                return ExitReason.PROFIT_TARGET

        # The setup this position was bought for is gone: regime went mixed
        # (candidate_direction "none") or flipped to the opposite side of
        # what we're holding. Either way the original reason to hold no
        # longer applies — exit before a price-based stop has to catch it.
        if current_setup is not None and current_setup.candidate_direction != position.contract.type:
            return ExitReason.THESIS_INVALIDATED

        if position.peak_premium is not None:
            activation = position.entry_premium * Decimal(str(1 + self.trailing_stop_activation_pct))
            gain_at_peak = position.peak_premium - position.entry_premium
            if position.peak_premium >= activation and gain_at_peak > 0:
                giveback_floor = position.entry_premium + gain_at_peak * Decimal(
                    str(1 - self.trailing_stop_giveback_pct)
                )
                if current_premium <= giveback_floor:
                    return ExitReason.TRAILING_STOP

        pnl_ratio = (current_premium - position.entry_premium) / position.entry_premium
        if pnl_ratio <= -Decimal(str(self.stop_loss_pct)):
            return ExitReason.STOP_LOSS

        if dte <= self.dte_floor:
            return ExitReason.DTE_STOP

        return None
