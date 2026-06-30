from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel


class RiskParams(BaseModel):
    max_concurrent_positions: int = 3
    max_premium_per_trade: Decimal = Decimal("500")  # per contract (mid × 100)
    daily_loss_kill_pct: float = 0.05               # 5 % of account NAV
    max_sector_concentration: int = 2


class RiskVerdict(BaseModel):
    approved: bool
    reasons: list[str] = []


class PortfolioState(BaseModel):
    open_positions: int = 0
    daily_pnl: Decimal = Decimal("0")
    account_nav: Decimal = Decimal("10000")
    sector_counts: dict[str, int] = {}
