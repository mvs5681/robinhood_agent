"""
Serialize the live agent's in-memory state (GEXCache) to backtest fixture files.

This module is intentionally self-contained — it imports only from the UW
schemas and the GEXCache.  It does not touch FlowWatcher, GEXScanner, or
run_pipeline, so existing agent logic is unchanged.

How it fits in:

    StateCaptureLoop runs as a fourth coroutine alongside scanner / watcher /
    capture_loop inside run_live.py.  At 4:30 PM ET each weekday it reads
    the GEXCache (already populated by GEXScanner for every ticker the agent
    is watching) and writes one YYYY-MM-DD/ directory to HISTORY_DIR.

    Because the cache contains the exact parsed objects the agent used when
    making decisions, the resulting fixtures replay the agent's actual view of
    the market — not a re-fetched approximation.

Fixture layout written (same schema DataStore expects):
    HISTORY_DIR/YYYY-MM-DD/
        market_tide.json
        flow_alerts.json
        {TICKER}_spot_gex.json
        {TICKER}_darkpool.json
        {TICKER}_net_prem_ticks.json
        {TICKER}_option_contracts.json
        {TICKER}_interpolated_iv.json
        {TICKER}_technicals_RSI.json
        {TICKER}_technicals_MACD.json
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from trader.uw.schemas import (
    DarkpoolPrint,
    FlowAlert,
    InterpolatedIVEntry,
    MarketTide,
    NetPremTick,
    OptionContract,
    SpotGEXByStrike,
    TechnicalPoint,
)

if TYPE_CHECKING:
    from trader.live.cache import GEXCache

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-type serialisers — convert parsed Pydantic objects back to the raw-JSON
# shape that DataStore / validators know how to re-parse.
# ---------------------------------------------------------------------------

def _ser_decimal(v) -> str | None:
    return str(v) if v is not None else None


def _ser_market_tide(items: list[MarketTide]) -> dict:
    rows = []
    for m in items:
        rows.append({
            "timestamp": m.timestamp.isoformat(),
            "net_call_premium": str(m.net_call_premium),
            "net_put_premium": str(m.net_put_premium),
            "net_volume": m.net_volume,
        })
    return {"data": rows}


def _ser_flow_alerts(items: list[FlowAlert]) -> dict:
    rows = []
    for a in items:
        rows.append({
            "ticker": a.ticker,
            "expiry": a.expiry.isoformat(),
            "strike": str(a.strike),
            "type": a.type,
            "total_premium": str(a.total_premium),
            "total_size": a.total_size,
            "volume": a.volume,
            "open_interest": a.open_interest,
            "alert_rule": a.alert_rule,
            "trade_count": a.trade_count,
            "underlying_price": _ser_decimal(a.underlying_price),
            "has_sweep": a.has_sweep,
            "has_floor": a.has_floor,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        })
    return {"data": rows}


def _ser_spot_gex(items: list[SpotGEXByStrike]) -> dict:
    rows = []
    for s in items:
        row: dict = {
            "price": str(s.price),
            "call_gamma_oi": str(s.call_gamma_oi),
            "put_gamma_oi": str(s.put_gamma_oi),
        }
        if s.call_gamma_vol is not None:
            row["call_gamma_vol"] = str(s.call_gamma_vol)
        if s.put_gamma_vol is not None:
            row["put_gamma_vol"] = str(s.put_gamma_vol)
        if s.call_delta_oi is not None:
            row["call_delta_oi"] = str(s.call_delta_oi)
        if s.put_delta_oi is not None:
            row["put_delta_oi"] = str(s.put_delta_oi)
        rows.append(row)
    return {"data": rows}


def _ser_darkpool(items: list[DarkpoolPrint]) -> dict:
    rows = []
    for d in items:
        rows.append({
            "ticker": d.ticker,
            "price": str(d.price),
            "size": d.size,
            "premium": str(d.premium),
            "executed_at": d.executed_at.isoformat(),
            "market_center": d.market_center,
            "canceled": d.canceled,
            "volume": d.volume,
        })
    return {"data": rows}


def _ser_net_prem_ticks(items: list[NetPremTick]) -> dict:
    rows = []
    for n in items:
        rows.append({
            "timestamp": n.timestamp.isoformat(),
            "net_call_premium": str(n.net_call_premium),
            "net_put_premium": str(n.net_put_premium),
            "net_volume": n.net_volume,
        })
    return {"data": rows}


def _ser_option_contracts(items: list[OptionContract]) -> dict:
    rows = []
    for c in items:
        rows.append({
            "ticker": c.ticker,
            "expiry": c.expiry.isoformat(),
            "strike": str(c.strike),
            "type": c.type,
            "bid": str(c.bid),
            "ask": str(c.ask),
            "open_interest": c.open_interest,
            "volume": c.volume,
            "implied_volatility": _ser_decimal(c.implied_volatility),
            "delta": _ser_decimal(c.delta),
            "gamma": _ser_decimal(c.gamma),
            "theta": _ser_decimal(c.theta),
            "vega": _ser_decimal(c.vega),
        })
    return {"data": rows}


def _ser_interpolated_iv(items: list[InterpolatedIVEntry]) -> dict:
    rows = []
    for iv in items:
        row: dict = {
            "days": iv.days,
            "volatility": str(iv.volatility),
            "percentile": str(iv.percentile),
        }
        if iv.implied_move_perc is not None:
            row["implied_move_perc"] = str(iv.implied_move_perc)
        if iv.trade_date is not None:
            row["date"] = iv.trade_date.isoformat()
        rows.append(row)
    return {"data": rows}


def _ser_technicals(items: list[TechnicalPoint]) -> dict:
    rows = []
    for p in items:
        row: dict = {"timestamp": p.timestamp}
        if p.value is not None:
            row["value"] = float(p.value)
        if p.macd is not None:
            row["macd"] = float(p.macd)
        if p.signal is not None:
            row["signal"] = float(p.signal)
        if p.histogram is not None:
            row["histogram"] = float(p.histogram)
        if p.upper_band is not None:
            row["upper_band"] = float(p.upper_band)
        if p.middle_band is not None:
            row["middle_band"] = float(p.middle_band)
        if p.lower_band is not None:
            row["lower_band"] = float(p.lower_band)
        rows.append(row)
    return {"data": rows}


# ---------------------------------------------------------------------------
# StateCapture
# ---------------------------------------------------------------------------


class StateCapture:
    """
    Write fixture files from the agent's live in-memory data.

    All public methods are synchronous and safe to call from a thread or
    directly from an async context via asyncio.to_thread.
    """

    def __init__(self, history_dir: Path | str) -> None:
        self._root = Path(history_dir)

    # ------------------------------------------------------------------
    # Primary entry point: read from GEXCache
    # ------------------------------------------------------------------

    def save_from_cache(
        self,
        cache: "GEXCache",
        trade_date: date,
        flow_alerts: list[FlowAlert] | None = None,
    ) -> int:
        """
        Serialize GEXCache → HISTORY_DIR/YYYY-MM-DD/ fixture files.

        Skips silently if the directory already exists and has a non-empty
        market_tide.json (idempotent — safe to call multiple times per day).

        Also updates HISTORY_DIR/ticker_coverage.json — a manifest of which
        dates each ticker was captured, so the backtest can select tickers
        with sufficient coverage without scanning every date directory.

        Returns the number of tickers written.
        """
        if not cache.tickers:
            logger.warning("StateCapture: cache is empty, nothing to save for %s", trade_date)
            return 0

        day_dir = self._root / trade_date.isoformat()
        market_tide_file = day_dir / "market_tide.json"
        if market_tide_file.exists():
            existing = json.loads(market_tide_file.read_text())
            if existing.get("data"):
                logger.debug("StateCapture: %s already captured, skipping", trade_date)
                return 0

        day_dir.mkdir(parents=True, exist_ok=True)

        # Market-wide files
        market_tide_file.write_text(
            json.dumps(_ser_market_tide(cache.market_tide))
        )
        (day_dir / "flow_alerts.json").write_text(
            json.dumps(_ser_flow_alerts(flow_alerts or []))
        )

        # Per-ticker files
        all_snapshots = cache.tickers   # dict[str, TickerSnapshot]
        for ticker, snap in all_snapshots.items():
            self._write_ticker(day_dir, ticker, snap)

        n = len(all_snapshots)
        logger.info(
            "StateCapture: saved %d ticker(s) for %s → %s",
            n, trade_date, day_dir,
        )

        self._update_coverage(set(all_snapshots.keys()), trade_date)
        return n

    def _update_coverage(self, tickers: set[str], trade_date: date) -> None:
        """Append trade_date to each ticker's entry in ticker_coverage.json."""
        coverage_file = self._root / "ticker_coverage.json"
        if coverage_file.exists():
            coverage: dict[str, list[str]] = json.loads(coverage_file.read_text())
        else:
            coverage = {}

        date_str = trade_date.isoformat()
        for ticker in tickers:
            dates = coverage.setdefault(ticker, [])
            if date_str not in dates:
                dates.append(date_str)
                dates.sort()

        coverage_file.write_text(json.dumps(coverage, indent=2))

    def covered_tickers(self, min_days: int = 1) -> dict[str, list[str]]:
        """
        Return tickers from ticker_coverage.json that have at least min_days
        of captured data.  Useful for selecting a backtest universe.

        Example:
            capture = StateCapture("data/history")
            universe = capture.covered_tickers(min_days=20)
            # {ticker: [date, ...]} for tickers with >= 20 days
        """
        coverage_file = self._root / "ticker_coverage.json"
        if not coverage_file.exists():
            return {}
        coverage: dict[str, list[str]] = json.loads(coverage_file.read_text())
        return {t: dates for t, dates in coverage.items() if len(dates) >= min_days}

    # ------------------------------------------------------------------
    # Per-ticker serialisation
    # ------------------------------------------------------------------

    def _write_ticker(self, day_dir: Path, ticker: str, snap) -> None:
        (day_dir / f"{ticker}_spot_gex.json").write_text(
            json.dumps(_ser_spot_gex(snap.spot_gex))
        )
        (day_dir / f"{ticker}_darkpool.json").write_text(
            json.dumps(_ser_darkpool(snap.darkpool))
        )
        (day_dir / f"{ticker}_net_prem_ticks.json").write_text(
            json.dumps(_ser_net_prem_ticks(snap.net_prem_ticks))
        )
        (day_dir / f"{ticker}_option_contracts.json").write_text(
            json.dumps(_ser_option_contracts(snap.option_contracts))
        )
        (day_dir / f"{ticker}_interpolated_iv.json").write_text(
            json.dumps(_ser_interpolated_iv(snap.interpolated_iv))
        )
        for fn, points in snap.technicals.items():
            (day_dir / f"{ticker}_technicals_{fn}.json").write_text(
                json.dumps(_ser_technicals(points))
            )


# ---------------------------------------------------------------------------
# StateCaptureLoop
# ---------------------------------------------------------------------------


class StateCaptureLoop:
    """
    Async coroutine that saves GEXCache → fixtures once per trading day at
    4:30 PM ET, after GEXScanner has completed its last full scan.

    This runs alongside the existing CaptureLoop (which re-fetches from UW).
    StateCaptureLoop writes the exact data the agent used — no extra API calls.
    """

    def __init__(
        self,
        cache: "GEXCache",
        capture: StateCapture,
        seed_tickers: list[str] | None = None,
    ) -> None:
        self._cache = cache
        self._capture = capture
        self._seeds = set(seed_tickers or [])

    async def run(self) -> None:
        from trader.live.capture_loop import _seconds_until_next_capture

        logger.info("StateCaptureLoop: started (seeds: %s)", sorted(self._seeds) or "none")
        while True:
            secs = _seconds_until_next_capture()
            logger.debug("StateCaptureLoop: sleeping %.0f s until next capture", secs)
            await asyncio.sleep(secs)

            trade_date = date.today()
            try:
                # Warn about any seed tickers absent from cache — these will
                # create gaps in the backtest fixture corpus.
                cached = set(self._cache.tickers.keys())
                missing = self._seeds - cached
                if missing:
                    logger.warning(
                        "StateCaptureLoop: seed ticker(s) absent from cache on %s — "
                        "backtest will have gaps: %s",
                        trade_date, sorted(missing),
                    )

                n = await asyncio.to_thread(
                    self._capture.save_from_cache,
                    self._cache,
                    trade_date,
                )
                if n:
                    logger.info(
                        "StateCaptureLoop: %s captured %d ticker(s) from cache",
                        trade_date, n,
                    )
            except Exception as exc:
                logger.error("StateCaptureLoop: capture failed for %s: %s", trade_date, exc)
