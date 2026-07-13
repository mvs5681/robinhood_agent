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
from collections import defaultdict
from decimal import Decimal
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
    from .config import LiveConfig

logger = logging.getLogger(__name__)

_SCAN_INTERVAL = 3600   # 1 hour between full scans
_RETRY_INTERVAL = 300   # 5 min retry after a scan error
_DEFAULT_MIN_PREMIUM = Decimal("250_000")   # $250K minimum premium to discover a ticker
_DEFAULT_MAX_TICKERS = 20                   # cap parallel fetches per scan cycle


class GEXScanner:
    """
    Runs hourly during market hours. Each cycle:
      1. Calls get_flow_alerts to discover which tickers have significant
         premium volume — no static watchlist needed.
      2. Merges with any seed_tickers that should always be scanned.
      3. Fetches slow-moving data per ticker and runs GEX detection.
      4. Writes results into the shared GEXCache.
    """

    def __init__(
        self,
        uw_tools: dict[str, BaseTool],
        cache: GEXCache,
        seed_tickers: list[str] | None = None,
        min_discovery_premium: Decimal = _DEFAULT_MIN_PREMIUM,
        max_discovered_tickers: int = _DEFAULT_MAX_TICKERS,
        detector_params: GEXDetectorParams | None = None,
        tel: TelemetryLogger | None = None,
        scan_interval: int = _SCAN_INTERVAL,
        config: "LiveConfig | None" = None,
    ) -> None:
        self._seed_tickers = list(seed_tickers or [])
        self.uw_tools = uw_tools
        self.cache = cache
        self._min_discovery_premium = min_discovery_premium
        self._max_discovered_tickers = max_discovered_tickers
        self.detector = GEXDetector(detector_params)
        self.tel = tel
        self.scan_interval = scan_interval
        self._config = config
        self._sem: asyncio.Semaphore | None = None  # created lazily inside event loop

    # Live-tunable settings — read from LiveConfig each cycle when provided,
    # so dashboard edits apply on the next scan without a restart.
    @property
    def seed_tickers(self) -> list[str]:
        return self._config.seed_tickers if self._config else self._seed_tickers

    @property
    def min_discovery_premium(self) -> Decimal:
        return self._config.discovery_min_premium if self._config else self._min_discovery_premium

    @property
    def max_discovered_tickers(self) -> int:
        return self._config.max_discovered_tickers if self._config else self._max_discovered_tickers

    async def run(self) -> None:
        """Main loop — runs forever, sleeping between scans."""
        logger.info("GEXScanner started — seed_tickers=%s discovery_min_premium=%s",
                    self.seed_tickers, self.min_discovery_premium)
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

    async def _discover_tickers(self) -> tuple[list[str], dict[str, Decimal]]:
        """
        Calls get_flow_alerts to find tickers with unusual premium volume.
        Returns (tickers, spot_hints) where spot_hints maps ticker → latest
        underlying_price from flow alerts (used as spot fallback for index
        tickers that have no darkpool prints).

        The UW MCP endpoint caps every response at 50 alerts and has no
        pagination, so a single unfiltered call surfaces only a handful of
        qualifying tickers. Instead we make one call per issue-type slice,
        each pre-filtered server-side to min_premium, so each slice gets its
        own 50-alert budget and indexes/ETFs aren't crowded out by stocks.
        """
        t0 = _time.monotonic()
        slices = [
            {"limit": 200, "min_premium": str(self.min_discovery_premium),
             "issue_types": ["Index", "ETF"]},
            {"limit": 200, "min_premium": str(self.min_discovery_premium),
             "issue_types": ["Common Stock", "ADR"]},
        ]
        results = await asyncio.gather(
            *[self.uw_tools["get_flow_alerts"].ainvoke(params) for params in slices],
            return_exceptions=True,
        )
        alerts = []
        seen_alerts: set[str] = set()
        failures = 0
        for params, result in zip(slices, results):
            if isinstance(result, Exception):
                failures += 1
                logger.error("discovery slice %s failed: %s", params["issue_types"], result)
                continue
            for a in parse_flow_alerts(result):
                key = f"{a.ticker}:{a.expiry}:{a.strike}:{a.type}:{a.created_at}"
                if key not in seen_alerts:
                    seen_alerts.add(key)
                    alerts.append(a)
        if failures == len(slices):
            logger.error("ticker discovery via get_flow_alerts failed: all slices errored")
            return self.seed_tickers[:], {}

        premium_by_ticker: dict[str, Decimal] = defaultdict(Decimal)
        spot_hints: dict[str, Decimal] = {}
        for alert in alerts:
            premium_by_ticker[alert.ticker] += alert.total_premium
            if alert.underlying_price and alert.ticker not in spot_hints:
                spot_hints[alert.ticker] = alert.underlying_price

        # Sort descending by total premium, apply threshold, cap count
        ranked = sorted(premium_by_ticker, key=lambda t: premium_by_ticker[t], reverse=True)
        discovered = [
            t for t in ranked
            if premium_by_ticker[t] >= self.min_discovery_premium
        ][:self.max_discovered_tickers]

        ms = round((_time.monotonic() - t0) * 1000, 1)
        logger.info(
            "Ticker discovery: %d alerts → %d candidates above $%s (%.0f ms)",
            len(alerts), len(discovered), self.min_discovery_premium, ms,
        )
        if self.tel:
            self.tel.uw_fetch(endpoint="get_flow_alerts (discovery)",
                              record_count=len(alerts), duration_ms=ms)

        # Seed tickers always first, then discovered (dedup preserving order)
        seen: set[str] = set()
        combined: list[str] = []
        for t in self.seed_tickers + discovered:
            if t not in seen:
                seen.add(t)
                combined.append(t)
        return combined, spot_hints

    async def _scan(self) -> None:
        t_start = _time.monotonic()

        # Discover universe from flow activity this hour
        tickers, spot_hints = await self._discover_tickers()
        logger.info("GEXScanner: scanning %d tickers: %s", len(tickers), tickers)

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

        # Per-ticker fetches in parallel (semaphore inside _scan_ticker limits concurrency)
        snapshots: dict[str, TickerSnapshot] = {}
        tasks = [self._scan_ticker(t, spot_hints.get(t)) for t in tickers]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for ticker, result in zip(tickers, results):
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

    async def _scan_ticker(self, ticker: str, spot_hint: Decimal | None = None) -> TickerSnapshot:
        snap = TickerSnapshot()
        # Lazily create semaphore inside the event loop (max 3 concurrent UW calls)
        if self._sem is None:
            self._sem = asyncio.Semaphore(3)

        async def _fetch(endpoint: str, kwargs: dict, parser):
            async with self._sem:
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

        snap.spot_gex = await _fetch("get_greek_exposure_by_strike",
                                     {"ticker": ticker}, parse_spot_gex_by_strike)
        snap.darkpool = await _fetch("get_dark_pool_trades",
                                     {"ticker_symbol": ticker, "limit": 100}, parse_darkpool_prints)
        snap.net_prem_ticks = await _fetch("get_flow_per_strike",
                                           {"ticker": ticker}, parse_net_prem_ticks)
        snap.option_contracts = await _fetch("get_options_chain",
                                             {"ticker": ticker, "limit": 50}, parse_option_contracts)

        technicals: dict = {}
        for fn in ("RSI", "MACD"):
            rows = await _fetch(
                "get_extended_technical_indicator",
                {"ticker": ticker, "function": fn, "interval": "daily"},
                lambda raw, f=fn: parse_technical_indicator(raw, f),
            )
            technicals[fn] = rows
        snap.technicals = technicals

        # Resolve spot price for GEX detection
        spot: Decimal | None = None
        # 1. Darkpool last print — most granular for equities
        if snap.darkpool:
            latest = max(snap.darkpool, key=lambda p: p.executed_at)
            spot = latest.price
        # 2. Flow alert underlying_price — covers index tickers with no darkpool
        if spot is None and spot_hint is not None:
            spot = spot_hint
            logger.debug("%s: using flow-alert spot hint %s", ticker, spot)

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
