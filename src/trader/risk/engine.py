from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from trader.scoring.schemas import CandidateSignal

from .schemas import PortfolioState, RiskParams, RiskVerdict

logger = logging.getLogger(__name__)

_DEFAULT_STATE_FILE = "logs/risk_state.json"


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

    state_file: path to a JSON file used to persist kill-switch and daily P&L
    across container restarts. Defaults to the RISK_STATE_FILE env var or
    logs/risk_state.json. The file is reset to a clean slate at midnight UTC
    (new trading day) so the kill-switch does not carry over between sessions.
    """

    def __init__(
        self,
        params: RiskParams | None = None,
        portfolio: PortfolioState | None = None,
        sector_map: dict[str, str] | None = None,
        open_positions_fn: Callable[[], int] | None = None,
        state_file: str | Path | None = None,
    ) -> None:
        self.params = params or RiskParams()
        self._sector_map: dict[str, str] = sector_map or {}
        self._kill_switch_active = False
        # Live position count source (e.g. PositionStore.count). Preferred over
        # the internal record_fill counter, which never decrements on exits.
        self._open_positions_fn = open_positions_fn

        p = portfolio or PortfolioState()
        self._open_positions: int = p.open_positions
        self._daily_pnl: Decimal = p.daily_pnl
        self._account_nav: Decimal = p.account_nav
        self._sector_counts: dict[str, int] = dict(p.sector_counts)

        # State persistence
        raw_path = state_file or os.environ.get("RISK_STATE_FILE", _DEFAULT_STATE_FILE)
        self._state_file: Path = Path(raw_path)
        self._load_state()

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

        open_positions = (
            self._open_positions_fn() if self._open_positions_fn is not None
            else self._open_positions
        )
        if open_positions >= self.params.max_concurrent_positions:
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
        self._persist_state()

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
            self._persist_state()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _today_utc(self) -> str:
        return date.today().isoformat()

    def _load_state(self) -> None:
        """Load persisted kill-switch and daily P&L from disk.

        Silently ignores missing files and parse errors (clean slate).
        Resets to a clean slate if the saved date is not today UTC —
        this handles midnight rollovers across container restarts.
        """
        if not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text())
        except Exception as exc:
            logger.warning("RiskEngine: could not read state file %s: %s", self._state_file, exc)
            return

        saved_date = data.get("date", "")
        if saved_date != self._today_utc():
            logger.info(
                "RiskEngine: state file is from %s (today is %s) — starting fresh",
                saved_date, self._today_utc(),
            )
            # Remove stale file so a clean one is written on the next persist
            try:
                self._state_file.unlink(missing_ok=True)
            except Exception:
                pass
            return

        try:
            self._daily_pnl = Decimal(str(data.get("daily_pnl", "0")))
            self._kill_switch_active = bool(data.get("kill_switch_active", False))
        except Exception as exc:
            logger.warning("RiskEngine: could not parse state from %s: %s", self._state_file, exc)
            return

        logger.info(
            "RiskEngine: loaded state from %s — daily_pnl=%s kill_switch=%s",
            self._state_file, self._daily_pnl, self._kill_switch_active,
        )

    def _persist_state(self) -> None:
        """Write kill-switch and daily P&L to disk so they survive restarts."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "date": self._today_utc(),
                "daily_pnl": str(self._daily_pnl),
                "kill_switch_active": self._kill_switch_active,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._state_file.write_text(json.dumps(payload, indent=2) + "\n")
        except Exception as exc:
            logger.error("RiskEngine: could not persist state to %s: %s", self._state_file, exc)
