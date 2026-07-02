"""GEX Scanner — hourly refresh of slow-moving per-ticker data.

Fetches: spot GEX, darkpool, net-prem ticks, option contracts, IV, technicals.
Runs GEXDetector to pre-compute GEXSetup for each ticker.
Writes results into the shared GEXCache.

The FlowWatcher reads from this cache and doesn't re-fetch these endpoints.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool

from trader.gex.detector import GEXDetector
from trader.gex.schemas import GEXDetectorParams
from trader.telemetry.logger import TelemetryLogger
from trader.uw.validators import (
    parse_darkpool_prints,
    parse_flow_alerts,
    parse_interpolated_iv,
    parse_market_tide,
    parse_net_prem_ticks,
    parse_option_contracts,
    parse_spot_gex_by_strike,
    parse_technical_indicator,
)

from .cache import GEXCache, TickerSnapshot
from .market_hours import is_market_hours, seconds_until_market_open

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_SCAN_INTERVAL = 3600   # 1 hour between full scans
_RETRY_INTERVAL = 300   # 5 min retry after a scan error


class GEXScanner:
    """
    Runs hourly during market hours, refreshing all slow-moving data and
    re-computing GEX setups. Writes into the shared GEXCache.
    """

    def __init__(
        self,
        tickers: list[str],
        uw_tools: dict[str, BaseTool],
        cache: GEXCache,
        detector_params: GEXDetectorParams | None = None,
        tel: TelemetryLogger | None = None,
        scan_interval: int = _SCAN_INTERVAL,
    ) -> None:
        self.tickers = tickers
        self.uw_tools = uw_tools
        self.cache = cache
        self.detector = GEXDetector(detector_params)
        self.tel = tel
        self.scan_interval = scan_interval

    async def run(self) -> None:
        """Main loop — runs forever, sleeping between scans."""
        logger.info("GEXScanner started for tickers: %s", self.tickers)
        while True:
            if not is_market_hours():
                wait = seconds_until_market_open()
                logger.info("Market closed — sleeping %.0f s until open", wait)
                await asyncio.sleep(max(wait, 60))
                continue

            try:
                await self._scan()
                logger.info("GEX scan complete — sleeping %d s", self.scan_interval)
                await asyncio.sleep(self.scan_interval)
            except Exception as exc:
                logger.error("GEX scan failed: %s — retry in %d s", exc, _RETRY_INTERVAL)
                await asyncio.sleep(_RETRY_INTERVAL)

    async def _scan(self) -> None:
        t_start = _time.monotonic()
        logger.info("GEXScanner: scanning %d tickers", len(self.tickers))

        # Market-wide data
        market_tide = []
        try:
            t0 = _time.monotonic()
            raw = await self.uw_tools["get_market_tide"].ainvoke({})
            market_tide = parse_market_tide(raw)
            ms = round((_time.monotonic() - t0) * 1000, 1)
            if self.tel:
                self.tel.uw_fetch(endpoint="get_market_tide",
                                  record_count=len(market_tide), duration_ms=ms)
        except Exception as exc:
            logger.error("market_tide fetch failed: %s", exc)

        # Per-ticker fetches in parallel
        snapshots: dict[str, TickerSnapshot] = {}
        tasks = [self._scan_ticker(t) for t in self.tickers]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for ticker, result in zip(self.tickers, results):
            if isinstance(result, Exception):
                logger.error("%s scan failed: %s", ticker, result)
            else:
                snapshots[ticker] = result

        await self.cache.update(market_tide, snapshots)
        logger.info(
            "GEXScanner: updated cache for %d tickers in %.1f s",
            len(snapshots),
            _time.monotonic() - t_start,
        )

    async def _scan_ticker(self, ticker: str) -> TickerSnapshot:
        snap = TickerSnapshot()

        async def _fetch(endpoint: str, kwargs: dict, parser):
            t0 = _time.monotonic()
            try:
                raw = await self.uw_tools[endpoint].ainvoke(kwargs)
                result = parser(raw)
                ms = round((_time.monotonic() - t0) * 1000, 1)
                if self.tel:
                    self.tel.uw_fetch(ticker=ticker, endpoint=endpoint,
                                      record_count=len(result), duration_ms=ms)
                return result
            except Exception as exc:
                logger.error("%s %s failed: %s", ticker, endpoint, exc)
                if self.tel:
                    ms = round((_time.monotonic() - t0) * 1000, 1)
                    self.tel.uw_fetch(ticker=ticker, endpoint=endpoint,
                                      record_count=0, duration_ms=ms, error=str(exc))
                return []

        snap.spot_gex = await _fetch("get_spot_exposures_by_strike",
                                     {"ticker": ticker}, parse_spot_gex_by_strike)
        snap.darkpool = await _fetch("get_darkpool_ticker",
                                     {"ticker": ticker}, parse_darkpool_prints)
        snap.net_prem_ticks = await _fetch("get_net_prem_ticks",
                                           {"ticker": ticker}, parse_net_prem_ticks)
        snap.option_contracts = await _fetch("get_option_contracts",
                                             {"ticker": ticker}, parse_option_contracts)
        snap.interpolated_iv = await _fetch("get_interpolated_iv",
                                            {"ticker": ticker}, parse_interpolated_iv)

        technicals: dict = {}
        for fn in ("RSI", "MACD"):
            rows = await _fetch(
                "get_technical_indicator",
                {"ticker": ticker, "function": fn, "interval": "daily"},
                lambda raw, f=fn: parse_technical_indicator(raw, f),
            )
            technicals[fn] = rows
        snap.technicals = technicals

        # Resolve spot price for GEX detection
        from decimal import Decimal
        spot: Decimal | None = None
        # Try darkpool first (most granular), then fall back to option contracts
        if snap.darkpool:
            latest = max(snap.darkpool, key=lambda p: p.executed_at)
            spot = latest.price
        if spot is None and snap.option_contracts:
            # Can't reliably get spot from contracts alone; skip detection
            pass

        # Also check any flow alerts for underlying_price (fetched in watcher)
        # GEX detection can run without spot if unavailable — watcher will retry
        if spot is not None and snap.spot_gex:
            t0 = _time.monotonic()
            try:
                import datetime as _dt
                snap.gex_setup = self.detector.detect(ticker, snap.spot_gex, spot)
                ms = round((_time.monotonic() - t0) * 1000, 1)
                s = snap.gex_setup
                logger.info(
                    "%s gex_setup: regime=%s direction=%s confidence=%.2f target=%s",
                    ticker, s.regime.value, s.candidate_direction,
                    s.structure_confidence, s.target_level,
                )
                if self.tel:
                    self.tel.gex_setup(
                        ticker=ticker,
                        regime=s.regime.value,
                        direction=s.candidate_direction,
                        setup_type=s.setup_type,
                        confidence=s.structure_confidence,
                        flip_point=float(s.flip_point) if s.flip_point else None,
                        target_level=float(s.target_level) if s.target_level else None,
                        duration_ms=ms,
                    )
            except Exception as exc:
                logger.error("%s detect_gex failed: %s", ticker, exc)

        from datetime import datetime, timezone
        snap.refreshed_at = datetime.now(timezone.utc)
        return snap
