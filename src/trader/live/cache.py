"""In-memory GEX cache shared between the scanner and watcher loops."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trader.gex.schemas import GEXSetup
    from trader.uw.schemas import (
        DarkpoolPrint,
        InterpolatedIVEntry,
        MarketTide,
        NetPremTick,
        OptionContract,
        SpotGEXByStrike,
        TechnicalPoint,
    )


@dataclass
class TickerSnapshot:
    """All slow-moving data for one ticker, refreshed hourly by GEXScanner."""

    spot_gex: list[SpotGEXByStrike] = field(default_factory=list)
    darkpool: list[DarkpoolPrint] = field(default_factory=list)
    net_prem_ticks: list[NetPremTick] = field(default_factory=list)
    option_contracts: list[OptionContract] = field(default_factory=list)
    interpolated_iv: list[InterpolatedIVEntry] = field(default_factory=list)
    technicals: dict[str, list[TechnicalPoint]] = field(default_factory=dict)
    gex_setup: GEXSetup | None = None
    refreshed_at: datetime = field(
        default_factory=lambda: datetime.fromtimestamp(0, tz=timezone.utc)
    )

    @property
    def is_stale(self) -> bool:
        age = (datetime.now(timezone.utc) - self.refreshed_at).total_seconds()
        return age > 3900  # >65 min — scanner interval is 60 min


@dataclass
class GEXCache:
    """
    Thread-safe in-memory cache shared between GEXScanner and FlowWatcher.

    GEXScanner writes; FlowWatcher reads. Protected by an asyncio.Lock.
    """

    market_tide: list[MarketTide] = field(default_factory=list)
    tickers: dict[str, TickerSnapshot] = field(default_factory=dict)
    last_scan: datetime | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def update(
        self,
        market_tide: list[MarketTide],
        snapshots: dict[str, TickerSnapshot],
    ) -> None:
        async with self._lock:
            self.market_tide = market_tide
            self.tickers.update(snapshots)
            self.last_scan = datetime.now(timezone.utc)

    async def snapshot(self, ticker: str) -> TickerSnapshot | None:
        async with self._lock:
            return self.tickers.get(ticker)

    async def all_snapshots(self) -> dict[str, TickerSnapshot]:
        async with self._lock:
            return dict(self.tickers)

    @property
    def ready(self) -> bool:
        return self.last_scan is not None and bool(self.tickers)
