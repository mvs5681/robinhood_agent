from __future__ import annotations

from decimal import Decimal

from trader.scoring.schemas import CandidateSignal

from .schemas import PortfolioState, RiskParams, RiskVerdict


class RiskEngine:
    """
    Synchronous hard-gate applied before any order is placed.

    Four gates in check() order:
      1. Kill-switch  — daily loss already exceeded; blocks everything
      2. Position cap — open positions < max_concurrent_positions
      3. Premium cap  — contract cost (mid × 100) ≤ max_premium_per_trade
      4. Sector conc  — open trades in same GICS sector < max_sector_concentration

    Kill-switch is permanent within a session: once tripped it cannot be reset
    regardless of subsequent record_pnl calls.

    sector_map: optional dict mapping ticker → GICS sector string.
    Tickers absent from the map skip the sector gate.
    """

    def __init__(
        self,
        params: RiskParams | None = None,
        portfolio: PortfolioState | None = None,
        sector_map: dict[str, str] | None = None,
    ) -> None:
        self.params = params or RiskParams()
        self._sector_map: dict[str, str] = sector_map or {}
        self._kill_switch_active = False

        p = portfolio or PortfolioState()
        self._open_positions: int = p.open_positions
        self._daily_pnl: Decimal = p.daily_pnl
        self._account_nav: Decimal = p.account_nav
        self._sector_counts: dict[str, int] = dict(p.sector_counts)

        # Pre-trip if injected portfolio already exceeds threshold
        self._evaluate_kill_threshold()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def check(self, candidate: CandidateSignal) -> RiskVerdict:
        if self._kill_switch_active:
            return RiskVerdict(
                approved=False,
                reasons=["kill_switch_active: daily loss limit reached"],
            )

        reasons: list[str] = []

        if self._open_positions >= self.params.max_concurrent_positions:
            reasons.append(
                f"max_concurrent_positions ({self.params.max_concurrent_positions}) reached"
            )

        contract = candidate.selected_contract
        if contract is not None:
            cost = contract.mid * 100
            if cost > self.params.max_premium_per_trade:
                reasons.append(
                    f"premium cost ${float(cost):.2f} exceeds cap "
                    f"${float(self.params.max_premium_per_trade):.2f}"
                )

        sector = self._sector_map.get(candidate.ticker)
        if sector is not None:
            count = self._sector_counts.get(sector, 0)
            if count >= self.params.max_sector_concentration:
                reasons.append(
                    f"sector '{sector}' at max concentration "
                    f"({self.params.max_sector_concentration})"
                )

        return RiskVerdict(approved=len(reasons) == 0, reasons=reasons)

    def record_fill(self, ticker: str, sector: str | None = None) -> None:
        """Call after an order is confirmed filled to update internal portfolio state."""
        self._open_positions += 1
        if sector:
            self._sector_counts[sector] = self._sector_counts.get(sector, 0) + 1

    def record_pnl(self, pnl: Decimal) -> None:
        """Accumulate realized P&L; may trip the kill-switch."""
        self._daily_pnl += pnl
        self._evaluate_kill_threshold()

    @property
    def kill_switch_active(self) -> bool:
        return self._kill_switch_active

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _evaluate_kill_threshold(self) -> None:
        if self._kill_switch_active:
            return  # can never be un-tripped
        if self._account_nav <= 0:
            return
        threshold = -(self._account_nav * Decimal(str(self.params.daily_loss_kill_pct)))
        if self._daily_pnl <= threshold:
            self._kill_switch_active = True
