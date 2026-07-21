#!/usr/bin/env python3
"""
Fetch historical options/equity data from Polygon.io and write backtest fixtures.

Output layout matches data/history/YYYY-MM-DD/ so run_backtest.py works unchanged:
  market_tide.json
  flow_alerts.json            (empty — not available historically)
  {TICKER}_spot_gex.json
  {TICKER}_option_contracts.json
  {TICKER}_darkpool.json      (empty — not available historically)
  {TICKER}_net_prem_ticks.json
  {TICKER}_technicals_RSI.json
  {TICKER}_technicals_MACD.json

Two modes depending on your Polygon plan:

  --free-tier   (5 calls/min)
    Only 3 API calls per ticker per day: VIX + equity OHLCV + options reference.
    Option mid-prices are computed via Black-Scholes (VIX as IV) instead of
    fetching per-contract OHLCV. Suitable for 1-2 tickers over months of data.
    Expected rate: ~3 calls/day → 12s gap between calls → ~36s/day.

  default (Starter $29/mo — unlimited)
    Also fetches per-contract OHLCV for real historical mid-prices.
    ~20-50 calls per ticker per day at 5 calls/sec.

Token: set POLYGON_API_KEY in .env or export it.

Usage (free tier — recommended starting point):
    python scripts/fetch_polygon_history.py \\
        --start 2025-01-02 \\
        --end 2025-03-31 \\
        --tickers SPY \\
        --free-tier \\
        --out data/history

Usage (paid Starter plan):
    python scripts/fetch_polygon_history.py \\
        --start 2025-01-02 \\
        --end 2025-06-30 \\
        --tickers SPY QQQ AAPL \\
        --out data/history
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import aiohttp

logger = logging.getLogger(__name__)

POLYGON_BASE = "https://api.polygon.io"
_DTE_MIN = 21
_DTE_MAX = 30
_TYPICAL_OI = 5_000    # OI proxy — magnitudes cancel in GEX regime ratio
_RSI_PERIOD = 14
_OHLCV_LOOKBACK_DAYS = 120


# ---------------------------------------------------------------------------
# Rate limiter — enforces calls/minute with a shared async lock
# ---------------------------------------------------------------------------

class _RateLimiter:
    def __init__(self, calls_per_min: int) -> None:
        self._min_gap = 60.0 / calls_per_min if calls_per_min > 0 else 0.0
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._min_gap - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()


# ---------------------------------------------------------------------------
# Black-Scholes helpers (no numpy required)
# ---------------------------------------------------------------------------

def _phi(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def _d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return 0.0
    return (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))


def bs_gamma(S: float, K: float, T: float, r: float = 0.05, sigma: float = 0.20) -> float:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = _d1(S, K, T, r, sigma)
    return _phi(d1) / (S * sigma * math.sqrt(T))


def bs_delta(S: float, K: float, T: float, r: float = 0.05, sigma: float = 0.20,
             is_call: bool = True) -> float:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 1.0 if is_call else -1.0
    d1 = _d1(S, K, T, r, sigma)
    return _cdf(d1) if is_call else _cdf(d1) - 1.0


def bs_price(S: float, K: float, T: float, r: float = 0.05, sigma: float = 0.20,
             is_call: bool = True) -> float:
    """Black-Scholes theoretical option price (used as mid in free-tier mode)."""
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if is_call else (K - S))
    d1 = _d1(S, K, T, r, sigma)
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * _cdf(d1) - K * math.exp(-r * T) * _cdf(d2)
    return K * math.exp(-r * T) * _cdf(-d2) - S * _cdf(-d1)


def bs_iv(mid: float, S: float, K: float, T: float, is_call: bool,
          r: float = 0.05) -> float:
    """Bisection-based IV solve. Returns 0.20 on failure."""
    if mid <= 0 or T <= 0 or S <= 0 or K <= 0:
        return 0.20

    def _p(sig: float) -> float:
        d1 = _d1(S, K, T, r, sig)
        d2 = d1 - sig * math.sqrt(T)
        if is_call:
            return S * _cdf(d1) - K * math.exp(-r * T) * _cdf(d2)
        return K * math.exp(-r * T) * _cdf(-d2) - S * _cdf(-d1)

    lo, hi = 0.01, 5.0
    for _ in range(60):
        m = (lo + hi) / 2
        if _p(m) > mid:
            hi = m
        else:
            lo = m
        if hi - lo < 0.0001:
            break
    return (lo + hi) / 2


# ---------------------------------------------------------------------------
# Technical indicators (pure Python, no pandas)
# ---------------------------------------------------------------------------

def _ema(vals: list[float], period: int) -> list[float]:
    if not vals:
        return []
    k = 2.0 / (period + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def compute_rsi(closes: list[float], period: int = _RSI_PERIOD) -> list[dict]:
    if len(closes) < period + 1:
        return []
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    rows: list[dict] = []
    for i in range(period, len(closes)):
        rs = avg_g / avg_l if avg_l else float("inf")
        rows.append({"timestamp": "", "value": round(100.0 - 100.0 / (1.0 + rs), 4)})
        if i < len(deltas):
            avg_g = (avg_g * (period - 1) + gains[i]) / period
            avg_l = (avg_l * (period - 1) + losses[i]) / period
    return rows


def compute_macd(closes: list[float], fast: int = 12, slow: int = 26,
                 sig_period: int = 9) -> list[dict]:
    if len(closes) < slow:
        return []
    ema_f = _ema(closes, fast)
    ema_s = _ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_f[slow - 1:], ema_s[slow - 1:])]
    signal = _ema(macd_line, sig_period)
    return [
        {
            "timestamp": "",
            "macd": round(m, 6),
            "signal": round(s, 6),
            "histogram": round(m - s, 6),
        }
        for m, s in zip(macd_line, signal)
    ]


# ---------------------------------------------------------------------------
# Polygon REST helpers
# ---------------------------------------------------------------------------

async def _get(session: aiohttp.ClientSession, path: str, params: dict,
               rl: _RateLimiter) -> dict:
    await rl.acquire()
    api_key = os.environ["POLYGON_API_KEY"]
    url = POLYGON_BASE + path
    all_params = {**params, "apiKey": api_key}
    try:
        async with session.get(url, params=all_params,
                               timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status == 429:
                logger.warning("Rate-limited — sleeping 15s")
                await asyncio.sleep(15)
                await rl.acquire()
                async with session.get(url, params=all_params,
                                       timeout=aiohttp.ClientTimeout(total=30)) as r2:
                    return await r2.json() if r2.status == 200 else {}
            if r.status != 200:
                logger.debug("Polygon %s → %d", path, r.status)
                return {}
            return await r.json()
    except Exception as exc:
        logger.error("Polygon %s failed: %s", path, exc)
        return {}


async def polygon_equity_ohlcv(session, ticker: str, from_date: date,
                               to_date: date, rl: _RateLimiter) -> list[dict]:
    data = await _get(
        session,
        f"/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}",
        {"adjusted": "true", "sort": "asc", "limit": 500},
        rl,
    )
    return data.get("results", [])


async def polygon_option_refs(session, underlying: str, exp_gte: date,
                              exp_lte: date, rl: _RateLimiter) -> list[dict]:
    """Return all reference contracts in a DTE window (includes expired contracts)."""
    results: list[dict] = []
    params: dict = {
        "underlying_ticker": underlying,
        "expiration_date.gte": exp_gte.isoformat(),
        "expiration_date.lte": exp_lte.isoformat(),
        "expired": "true",   # include contracts that have already expired (required for historical replay)
        "limit": 1000,
    }
    while True:
        data = await _get(session, "/v3/reference/options/contracts", params, rl)
        batch = data.get("results", [])
        results.extend(batch)
        next_url = data.get("next_url", "")
        if not next_url or not batch:
            break
        cursor = next_url.split("cursor=")[-1].split("&")[0] if "cursor=" in next_url else ""
        if not cursor:
            break
        params = {"cursor": cursor, "limit": 1000}
    return results


async def polygon_option_ohlcv(session, option_ticker: str, trade_date: date,
                               rl: _RateLimiter) -> dict | None:
    data = await _get(
        session,
        f"/v2/aggs/ticker/{option_ticker}/range/1/day/{trade_date}/{trade_date}",
        {"adjusted": "false", "limit": 1},
        rl,
    )
    results = data.get("results", [])
    return results[0] if results else None


async def polygon_vix(session, trade_date: date, rl: _RateLimiter) -> float:
    """VIX close as an IV proxy for the day (fallback: 20%)."""
    data = await _get(
        session,
        f"/v2/aggs/ticker/I:VIX/range/1/day/{trade_date}/{trade_date}",
        {"adjusted": "false", "limit": 1},
        rl,
    )
    results = data.get("results", [])
    return results[0].get("c", 20.0) / 100.0 if results else 0.20


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _spot_gex_rows(ref_contracts: list[dict], spot: float, sigma: float,
                   trade_date: date) -> list[dict]:
    by_strike: dict[float, dict] = {}
    for c in ref_contracts:
        strike = float(c.get("strike_price", 0) or 0)
        if strike <= 0:
            continue
        try:
            exp_date = date.fromisoformat(c.get("expiration_date", ""))
        except ValueError:
            continue
        T = max((exp_date - trade_date).days, 0) / 365.0
        gamma = bs_gamma(spot, strike, T, sigma=sigma)
        gex = gamma * _TYPICAL_OI * 100.0
        ctype = (c.get("contract_type") or "call").lower()
        row = by_strike.setdefault(strike, {"price": strike, "call_gamma_oi": 0.0,
                                             "put_gamma_oi": 0.0})
        if ctype == "call":
            row["call_gamma_oi"] += gex
        else:
            row["put_gamma_oi"] += gex
    return sorted(by_strike.values(), key=lambda r: r["price"])


def _option_contract_rows_bs(ref_contracts: list[dict], spot: float,
                              sigma: float, trade_date: date) -> list[dict]:
    """Free-tier mode: mid price from Black-Scholes, no OHLCV calls needed."""
    rows = []
    for c in ref_contracts:
        ot = c.get("ticker", "")
        strike = float(c.get("strike_price", 0) or 0)
        ctype = (c.get("contract_type") or "call").lower()
        exp_str = c.get("expiration_date", "")
        if not ot or strike <= 0:
            continue
        try:
            exp_date = date.fromisoformat(exp_str)
        except ValueError:
            continue
        T = max((exp_date - trade_date).days, 0) / 365.0
        is_call = ctype == "call"
        mid = bs_price(spot, strike, T, sigma=sigma, is_call=is_call)
        if mid <= 0.01:
            continue  # deep OTM/expired — skip
        delta = bs_delta(spot, strike, T, sigma=sigma, is_call=is_call)
        gamma = bs_gamma(spot, strike, T, sigma=sigma)
        occ = ot.replace("O:", "") if ot.startswith("O:") else ot
        rows.append({
            "option_symbol": occ,
            "bid": round(mid * 0.97, 4),
            "ask": round(mid * 1.03, 4),
            "open_interest": _TYPICAL_OI,
            "volume": 0,
            "implied_volatility": round(sigma, 5),
            "delta": round(delta, 5),
            "gamma": round(gamma, 6),
            "theta": 0.0,
            "vega": 0.0,
        })
    return rows


def _option_contract_rows_ohlcv(ref_contracts: list[dict],
                                 ohlcv_by_ticker: dict[str, dict],
                                 spot: float, sigma: float,
                                 trade_date: date) -> list[dict]:
    """Paid mode: real mid from per-contract OHLCV, IV solved via bisection."""
    rows = []
    for c in ref_contracts:
        ot = c.get("ticker", "")
        strike = float(c.get("strike_price", 0) or 0)
        ctype = (c.get("contract_type") or "call").lower()
        exp_str = c.get("expiration_date", "")
        if not ot or strike <= 0:
            continue
        try:
            exp_date = date.fromisoformat(exp_str)
        except ValueError:
            continue
        ohlcv = ohlcv_by_ticker.get(ot)
        if ohlcv is None:
            continue
        h = ohlcv.get("h", 0) or 0
        lo = ohlcv.get("l", 0) or 0
        op = ohlcv.get("o", 0) or 0
        cl = ohlcv.get("c", 0) or 0
        mid = (h + lo) / 2.0 if h and lo else (op + cl) / 2.0
        if mid <= 0:
            continue
        T = max((exp_date - trade_date).days, 0) / 365.0
        is_call = ctype == "call"
        iv = bs_iv(mid, spot, strike, T, is_call=is_call)
        delta = bs_delta(spot, strike, T, sigma=iv, is_call=is_call)
        gamma = bs_gamma(spot, strike, T, sigma=iv)
        occ = ot.replace("O:", "") if ot.startswith("O:") else ot
        rows.append({
            "option_symbol": occ,
            "bid": round(lo * 0.99, 4) if lo else round(mid * 0.98, 4),
            "ask": round(h * 1.01, 4) if h else round(mid * 1.02, 4),
            "open_interest": _TYPICAL_OI,
            "volume": int(ohlcv.get("v", 0) or 0),
            "implied_volatility": round(iv, 5),
            "delta": round(delta, 5),
            "gamma": round(gamma, 6),
            "theta": 0.0,
            "vega": 0.0,
        })
    return rows


def _net_prem_rows(contract_rows: list[dict], trade_date: date) -> list[dict]:
    """Aggregate net premium per strike from contract rows (works for both modes)."""
    by_strike: dict[float, dict] = {}
    for r in contract_rows:
        occ = r.get("option_symbol", "")
        if not occ or len(occ) < 15:
            continue
        # Parse OCC: SPY250620C00560000 → type at [-9], strike at [-8:]
        ctype = "call" if occ[-9].upper() == "C" else "put"
        try:
            strike = int(occ[-8:]) / 1000.0
        except ValueError:
            continue
        mid = (r.get("bid", 0) + r.get("ask", 0)) / 2.0
        vol = r.get("volume", 0) or _TYPICAL_OI // 10  # proxy volume in BS mode
        prem = mid * vol * 100
        row = by_strike.setdefault(strike, {"timestamp": trade_date.isoformat(),
                                             "net_call_premium": 0.0,
                                             "net_put_premium": 0.0})
        if ctype == "call":
            row["net_call_premium"] += prem
        else:
            row["net_put_premium"] += prem
    return list(by_strike.values())


def _market_tide_row(contract_rows: list[dict], trade_date: date) -> list[dict]:
    """Single aggregate record from call vs put net premium in contract_rows."""
    call_prem = sum(
        (r.get("bid", 0) + r.get("ask", 0)) / 2 * (r.get("volume", 0) or _TYPICAL_OI // 10) * 100
        for r in contract_rows
        if r.get("option_symbol", "")[-9:-8].upper() == "C"
    )
    put_prem = sum(
        (r.get("bid", 0) + r.get("ask", 0)) / 2 * (r.get("volume", 0) or _TYPICAL_OI // 10) * 100
        for r in contract_rows
        if r.get("option_symbol", "")[-9:-8].upper() == "P"
    )
    return [{
        "timestamp": trade_date.isoformat(),
        "net_call_premium": round(call_prem, 2),
        "net_put_premium": round(put_prem, 2),
        "net_volume": 0,
    }]


def _spot_darkpool_entry(ticker: str, spot: float, trade_date: date) -> dict:
    """Synthetic darkpool print that carries the EOD spot price into the DataStore."""
    return {
        "ticker": ticker,
        "price": round(spot, 4),
        "size": 1,
        "premium": round(spot, 4),
        "executed_at": f"{trade_date.isoformat()}T21:00:00+00:00",
        "market_center": "POLYGON_PROXY",
    }


# ---------------------------------------------------------------------------
# Per-ticker work for one day
# ---------------------------------------------------------------------------

async def _process_ticker(
    session: aiohttp.ClientSession,
    ticker: str,
    trade_date: date,
    day_dir: Path,
    equity_ohlcv: list[dict],
    sigma: float,
    rl: _RateLimiter,
    free_tier: bool,
) -> list[dict]:
    """
    Fetch + write all per-ticker fixture files for one day.
    Returns the list of contract rows (used by caller for market_tide if ticker==SPY).
    """
    spot = float(equity_ohlcv[-1].get("c", 0)) if equity_ohlcv else 0.0
    if spot <= 0:
        logger.warning("%s %s: no spot price, writing empty fixtures", ticker, trade_date)
        _write_empty_ticker(day_dir, ticker)
        _write_technicals(day_dir, ticker, equity_ohlcv, trade_date)
        return []

    exp_gte = trade_date + timedelta(days=_DTE_MIN)
    exp_lte = trade_date + timedelta(days=_DTE_MAX)
    refs = await polygon_option_refs(session, ticker, exp_gte, exp_lte, rl)
    if not refs:
        logger.info("%s %s: 0 contracts in DTE %d–%d", ticker, trade_date, _DTE_MIN, _DTE_MAX)
        _write_empty_ticker(day_dir, ticker)
        _write_technicals(day_dir, ticker, equity_ohlcv, trade_date)
        return []

    if free_tier:
        contract_rows = _option_contract_rows_bs(refs, spot, sigma, trade_date)
    else:
        # Paid mode: fetch per-contract OHLCV in parallel
        option_tickers = [c["ticker"] for c in refs if c.get("ticker")]
        ohlcv_results = await asyncio.gather(
            *[polygon_option_ohlcv(session, ot, trade_date, rl) for ot in option_tickers],
            return_exceptions=True,
        )
        ohlcv_by_ticker: dict[str, dict] = {}
        for c, res in zip(refs, ohlcv_results):
            if not isinstance(res, Exception) and res is not None:
                ohlcv_by_ticker[c.get("ticker", "")] = res
        contract_rows = _option_contract_rows_ohlcv(refs, ohlcv_by_ticker, spot, sigma, trade_date)

    gex_rows = _spot_gex_rows(refs, spot, sigma, trade_date)
    (day_dir / f"{ticker}_spot_gex.json").write_text(json.dumps({"data": gex_rows}))
    (day_dir / f"{ticker}_option_contracts.json").write_text(
        json.dumps({"data": contract_rows}))
    (day_dir / f"{ticker}_net_prem_ticks.json").write_text(
        json.dumps({"data": _net_prem_rows(contract_rows, trade_date)}))
    (day_dir / f"{ticker}_darkpool.json").write_text(
        json.dumps({"data": [_spot_darkpool_entry(ticker, spot, trade_date)]}))
    _write_technicals(day_dir, ticker, equity_ohlcv, trade_date)

    logger.info("%s %s: %d refs → %d contract rows (spot=%.2f VIX=%.1f%%)",
                ticker, trade_date, len(refs), len(contract_rows), spot, sigma * 100)
    return contract_rows


def _write_empty_ticker(day_dir: Path, ticker: str) -> None:
    for fname in (f"{ticker}_spot_gex.json", f"{ticker}_option_contracts.json",
                  f"{ticker}_net_prem_ticks.json", f"{ticker}_darkpool.json"):
        (day_dir / fname).write_text('{"data": []}')


def _write_technicals(day_dir: Path, ticker: str, equity_ohlcv: list[dict],
                      trade_date: date) -> None:
    closes = [float(r["c"]) for r in equity_ohlcv if r.get("c")]
    rsi_rows = compute_rsi(closes)
    macd_rows = compute_macd(closes)
    if rsi_rows:
        rsi_rows[-1]["timestamp"] = trade_date.isoformat()
    if macd_rows:
        macd_rows[-1]["timestamp"] = trade_date.isoformat()
    (day_dir / f"{ticker}_technicals_RSI.json").write_text(json.dumps({"data": rsi_rows}))
    (day_dir / f"{ticker}_technicals_MACD.json").write_text(json.dumps({"data": macd_rows}))


# ---------------------------------------------------------------------------
# Per-day orchestration
# ---------------------------------------------------------------------------

async def _fetch_day(
    session: aiohttp.ClientSession,
    trade_date: date,
    tickers: list[str],
    out_dir: Path,
    rl: _RateLimiter,
    free_tier: bool,
) -> None:
    day_dir = out_dir / trade_date.isoformat()
    if (day_dir / "market_tide.json").exists():
        logger.info("%s already fetched — skipping", trade_date)
        return

    day_dir.mkdir(parents=True, exist_ok=True)
    logger.info("=== %s ===", trade_date)

    sigma = await polygon_vix(session, trade_date, rl)
    logger.debug("%s VIX=%.1f%%", trade_date, sigma * 100)

    # Equity OHLCV lookback (all tickers + SPY for market tide)
    lookback_start = trade_date - timedelta(days=_OHLCV_LOOKBACK_DAYS)
    all_equity = list(dict.fromkeys(tickers + ["SPY"]))
    # In free-tier mode keep equity fetches sequential to respect rate limit
    equity_ohlcv: dict[str, list[dict]] = {}
    for t in all_equity:
        res = await polygon_equity_ohlcv(session, t, lookback_start, trade_date, rl)
        equity_ohlcv[t] = res if isinstance(res, list) else []

    (day_dir / "flow_alerts.json").write_text('{"data": []}')

    spy_rows: list[dict] = []
    for ticker in tickers:
        rows = await _process_ticker(
            session, ticker, trade_date, day_dir,
            equity_ohlcv.get(ticker, []), sigma, rl, free_tier,
        )
        if ticker == "SPY":
            spy_rows = rows

    # market_tide from SPY contract rows (or zeros if SPY not in tickers)
    if not spy_rows and "SPY" not in tickers:
        # Fetch SPY refs for tide only (free-tier: 1 extra call)
        spy_refs = await polygon_option_refs(
            session, "SPY",
            trade_date + timedelta(days=_DTE_MIN),
            trade_date + timedelta(days=_DTE_MAX),
            rl,
        )
        spy_spot = float(equity_ohlcv.get("SPY", [{}])[-1].get("c", 0))
        if spy_refs and spy_spot:
            spy_rows = _option_contract_rows_bs(spy_refs, spy_spot, sigma, trade_date)

    tide = _market_tide_row(spy_rows, trade_date)
    (day_dir / "market_tide.json").write_text(json.dumps({"data": tide}))
    logger.info("%s complete", trade_date)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _trading_days(start: date, end: date) -> list[date]:
    days: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _estimate_runtime(n_days: int, n_tickers: int, calls_per_min: int,
                      free_tier: bool) -> str:
    if free_tier:
        # VIX + equity×(N+1 with SPY) + refs×(N + optional SPY tide)
        calls_per_day = 1 + (n_tickers + 1) + n_tickers + (1 if n_tickers > 0 else 0)
    else:
        calls_per_day = 1 + (n_tickers + 1) + n_tickers * (1 + 30)  # ~30 contracts avg
    total_calls = calls_per_day * n_days
    secs = total_calls * (60.0 / calls_per_min)
    if secs < 120:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs/60:.0f} min"
    return f"{secs/3600:.1f} hr"


async def _main(args: argparse.Namespace) -> None:
    if not os.environ.get("POLYGON_API_KEY"):
        logger.error("POLYGON_API_KEY not set — add it to .env or export it")
        sys.exit(1)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    tickers = args.tickers or ["SPY"]
    free_tier = args.free_tier

    calls_per_min = args.calls_per_min if args.calls_per_min else (5 if free_tier else 300)
    rl = _RateLimiter(calls_per_min)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    days = _trading_days(start, end)

    eta = _estimate_runtime(len(days), len(tickers), calls_per_min, free_tier)
    mode = "FREE TIER (BS pricing, no contract OHLCV)" if free_tier else "PAID (per-contract OHLCV)"
    logger.info("Mode: %s  |  %d calls/min  |  ETA ~%s", mode, calls_per_min, eta)
    logger.info("Fetching %d trading days (%s → %s) for: %s", len(days), start, end,
                " ".join(tickers))
    logger.info("Output: %s", out_dir.resolve())

    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        for d in days:
            await _fetch_day(session, d, tickers, out_dir, rl, free_tier)

    logger.info("Done.  Run backtest:")
    logger.info("  python scripts/run_backtest.py --fixtures %s --start %s --end %s --tickers %s",
                out_dir, start, end, " ".join(tickers))


def main() -> None:
    p = argparse.ArgumentParser(
        description="Fetch Polygon.io historical data for GEX backtest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--start", required=True, metavar="YYYY-MM-DD")
    p.add_argument("--end", required=True, metavar="YYYY-MM-DD")
    p.add_argument("--tickers", nargs="+", metavar="TICKER",
                   help="Tickers to fetch (default: SPY)")
    p.add_argument("--out", default="data/history", metavar="DIR")
    p.add_argument(
        "--free-tier",
        action="store_true",
        help="Use Black-Scholes pricing instead of per-contract OHLCV (5 calls/min limit)",
    )
    p.add_argument(
        "--calls-per-min",
        type=int,
        default=None,
        metavar="N",
        help="Override rate limit (default: 5 for --free-tier, 300 otherwise)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
