from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from .schemas import ExitReason, ExitSignal, Position


class ExitMonitor:
    """
    Evaluates an open position on each monitoring tick and returns an ExitSignal
    if any exit condition is triggered.

    Priority order (first match wins):
      1. Profit target — underlying price reached the GEX gamma wall
      2. Stop loss     — option premium dropped ≥ stop_loss_pct from entry
      3. DTE stop      — dte_remaining ≤ dte_floor (avoid final-week decay)

    Fully synchronous; no I/O.
    """

    def __init__(self, stop_loss_pct: float = 0.35, dte_floor: int = 7) -> None:
        self.stop_loss_pct = stop_loss_pct
        self.dte_floor = dte_floor

    def evaluate(
        self,
        position: Position,
        current_price: Decimal,    # current underlying price
        current_premium: Decimal,  # current option mid (per share)
        dte: int,
        as_of: datetime | None = None,
    ) -> ExitSignal | None:
        reason = self._first_triggered(position, current_price, current_premium, dte)
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
    ) -> ExitReason | None:
        if current_price >= position.target_level:
            return ExitReason.PROFIT_TARGET

        pnl_ratio = (current_premium - position.entry_premium) / position.entry_premium
        if pnl_ratio <= -Decimal(str(self.stop_loss_pct)):
            return ExitReason.STOP_LOSS

        if dte <= self.dte_floor:
            return ExitReason.DTE_STOP

        return None
