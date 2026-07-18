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

What Polygon provides vs what requires approximation:
  ✓ Equity spot price (OHLCV)
  ✓ Historical option mid-prices (per-contract OHLCV)
  ✓ Options chain structure (strikes, expiries, contract types)
  ✓ RSI and MACD (computed from equity OHLCV)
  ~ GEX by strike — gamma computed via Black-Scholes (VIX as IV proxy), OI
    approximated as 5000 contracts; ratios (used for regime detection) are valid
  ✗ Flow alerts / darkpool — written as empty; blend scorer treats these as neutral

Requirements: Polygon Starter ($29/mo) for unlimited historical options OHLCV.
Token: set POLYGON_API_KEY in .env or export it.

Usage:
    python scripts/fetch_polygon_history.py \\
        --start 2025-01-15 \\
        --end 2025-06-30 \\
        --tickers SPY QQQ AAPL \\
        --out data/history

Then run the backtest:
    python scripts/run_backtest.py \\
        --fixtures data/history \\
        --start 2025-01-15 \\
        --end 2025-06-30 \\
        --tickers SPY QQQ AAPL
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
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
_SEM = 5          # Starter plan allows ~5 req/sec burst
_DTE_MIN = 21
_DTE_MAX = 30
_TYPICAL_OI = 5_000   # OI proxy — magnitudes cancel in regime ratio
_RSI_PERIOD = 14
_OHLCV_LOOKBACK_DAYS = 120  # equity OHLCV history needed for technicals


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
        return (1.0 if is_call else -1.0)
    d1 = _d1(S, K, T, r, sigma)
    return _cdf(d1) if is_call else _cdf(d1) - 1.0


def bs_iv(mid: float, S: float, K: float, T: float, is_call: bool,
          r: float = 0.05) -> float:
    """Bisection-based IV solve. Returns 0.20 on failure."""
    if mid <= 0 or T <= 0 or S <= 0 or K <= 0:
        return 0.20

    def _price(sig: float) -> float:
        d1 = _d1(S, K, T, r, sig)
        d2 = d1 - sig * math.sqrt(T)
        if is_call:
            return S * _cdf(d1) - K * math.exp(-r * T) * _cdf(d2)
        return K * math.exp(-r * T) * _cdf(-d2) - S * _cdf(-d1)

    lo, hi = 0.01, 5.0
    for _ in range(60):
        mid_sig = (lo + hi) / 2
        if _price(mid_sig) > mid:
            hi = mid_sig
        else:
            lo = mid_sig
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
               sem: asyncio.Semaphore) -> dict:
    api_key = os.environ["POLYGON_API_KEY"]
    url = POLYGON_BASE + path
    all_params = {**params, "apiKey": api_key}
    async with sem:
        async with session.get(url, params=all_params, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status == 429:
                logger.warning("Rate-limited by Polygon — sleeping 12s")
                await asyncio.sleep(12)
                async with session.get(url, params=all_params,
                                       timeout=aiohttp.ClientTimeout(total=30)) as r2:
                    return await r2.json() if r2.status == 200 else {}
            if r.status != 200:
                body = await r.text()
                logger.debug("Polygon %s → %d: %.200s", path, r.status, body)
                return {}
            return await r.json()


async def polygon_equity_ohlcv(session, ticker: str, from_date: date,
                               to_date: date, sem) -> list[dict]:
    data = await _get(
        session,
        f"/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}",
        {"adjusted": "true", "sort": "asc", "limit": 500},
        sem,
    )
    return data.get("results", [])


async def polygon_option_refs(session, underlying: str, exp_gte: date,
                              exp_lte: date, sem) -> list[dict]:
    """Return all reference contracts in a DTE window (includes expired contracts)."""
    results: list[dict] = []
    params: dict = {
        "underlying_ticker": underlying,
        "expiration_date.gte": exp_gte.isoformat(),
        "expiration_date.lte": exp_lte.isoformat(),
        "limit": 1000,
    }
    while True:
        data = await _get(session, "/v3/reference/options/contracts", params, sem)
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
                               sem) -> dict | None:
    data = await _get(
        session,
        f"/v2/aggs/ticker/{option_ticker}/range/1/day/{trade_date}/{trade_date}",
        {"adjusted": "false", "limit": 1},
        sem,
    )
    results = data.get("results", [])
    return results[0] if results else None


async def polygon_vix(session, trade_date: date, sem) -> float:
    """VIX close as an IV proxy for the day (fallback: 20%)."""
    data = await _get(
        session,
        f"/v2/aggs/ticker/I:VIX/range/1/day/{trade_date}/{trade_date}",
        {"adjusted": "false", "limit": 1},
        sem,
    )
    results = data.get("results", [])
    if results:
        return results[0].get("c", 20.0) / 100.0
    return 0.20


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _spot_gex_rows(ref_contracts: list[dict], spot: float, sigma: float,
                   trade_date: date) -> list[dict]:
    """Compute GEX per strike via BS gamma × _TYPICAL_OI × 100."""
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


def _option_contract_rows(ref_contracts: list[dict],
                          ohlcv_by_ticker: dict[str, dict],
                          spot: float, sigma: float, trade_date: date) -> list[dict]:
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
            continue  # no traded price that day

        h = ohlcv.get("h", 0) or 0
        lo = ohlcv.get("l", 0) or 0
        op = ohlcv.get("o", 0) or 0
        cl = ohlcv.get("c", 0) or 0
        mid = (h + lo) / 2.0 if h and lo else (op + cl) / 2.0
        if mid <= 0:
            continue

        T = max((exp_date - trade_date).days, 0) / 365.0
        iv = bs_iv(mid, spot, strike, T, is_call=(ctype == "call"))
        delta = bs_delta(spot, strike, T, sigma=iv, is_call=(ctype == "call"))
        gamma = bs_gamma(spot, strike, T, sigma=iv)

        # OCC symbol from Polygon ticker  O:SPY250620C00560000 → SPY250620C00560000
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


def _net_prem_rows(contracts_ohlcv: list[tuple[dict, dict]],
                   trade_date: date) -> list[dict]:
    """One row per strike aggregating call/put premium volume."""
    by_strike: dict[float, dict] = {}
    for c, ohlcv in contracts_ohlcv:
        strike = float(c.get("strike_price", 0) or 0)
        ctype = (c.get("contract_type") or "call").lower()
        h = ohlcv.get("h", 0) or 0
        lo = ohlcv.get("l", 0) or 0
        mid = (h + lo) / 2.0
        vol = ohlcv.get("v", 0) or 0
        prem = mid * vol * 100
        row = by_strike.setdefault(strike, {"timestamp": trade_date.isoformat(),
                                             "net_call_premium": 0.0,
                                             "net_put_premium": 0.0})
        if ctype == "call":
            row["net_call_premium"] += prem
        else:
            row["net_put_premium"] += prem
    return list(by_strike.values())


def _market_tide_row(spy_pairs: list[tuple[dict, dict]], trade_date: date) -> list[dict]:
    """Single aggregate record from SPY call vs put premium volume."""
    net_call = sum(
        ((o.get("h", 0) or 0) + (o.get("l", 0) or 0)) / 2 * (o.get("v", 0) or 0) * 100
        for c, o in spy_pairs
        if o and (c.get("contract_type") or "").lower() == "call"
    )
    net_put = sum(
        ((o.get("h", 0) or 0) + (o.get("l", 0) or 0)) / 2 * (o.get("v", 0) or 0) * 100
        for c, o in spy_pairs
        if o and (c.get("contract_type") or "").lower() == "put"
    )
    return [{
        "timestamp": trade_date.isoformat(),
        "net_call_premium": round(net_call, 2),
        "net_put_premium": round(net_put, 2),
        "net_volume": 0,
    }]


# ---------------------------------------------------------------------------
# Per-ticker work for one day
# ---------------------------------------------------------------------------

async def _process_ticker(
    session: aiohttp.ClientSession,
    ticker: str,
    trade_date: date,
    day_dir: Path,
    equity_ohlcv: list[dict],  # lookback history ending on trade_date
    sigma: float,
    sem: asyncio.Semaphore,
) -> list[tuple[dict, dict]]:
    """
    Fetch + write all per-ticker fixture files for one trading day.
    Returns (contract_ref, ohlcv) pairs (used by caller to build market_tide).
    """
    # Spot from closing price on trade_date (last row in the lookback OHLCV)
    spot = float(equity_ohlcv[-1].get("c", 0)) if equity_ohlcv else 0.0
    if spot <= 0:
        logger.warning("%s %s: no spot price, writing empty fixtures", ticker, trade_date)
        _write_empty_ticker(day_dir, ticker)
        _write_technicals(day_dir, ticker, equity_ohlcv, trade_date)
        return []

    # Options reference data for DTE 21–30 on trade_date
    exp_gte = trade_date + timedelta(days=_DTE_MIN)
    exp_lte = trade_date + timedelta(days=_DTE_MAX)
    refs = await polygon_option_refs(session, ticker, exp_gte, exp_lte, sem)
    if not refs:
        logger.info("%s %s: 0 contracts in DTE %d–%d, writing empty", ticker, trade_date,
                    _DTE_MIN, _DTE_MAX)
        _write_empty_ticker(day_dir, ticker)
        _write_technicals(day_dir, ticker, equity_ohlcv, trade_date)
        return []

    # Historical option OHLCV for each contract on trade_date (parallel)
    option_tickers = [c["ticker"] for c in refs if c.get("ticker")]
    ohlcv_results = await asyncio.gather(
        *[polygon_option_ohlcv(session, ot, trade_date, sem) for ot in option_tickers],
        return_exceptions=True,
    )
    ohlcv_by_ticker: dict[str, dict] = {}
    pairs: list[tuple[dict, dict]] = []
    for ref, res in zip(refs, ohlcv_results):
        ot = ref.get("ticker", "")
        if isinstance(res, Exception) or res is None:
            continue
        ohlcv_by_ticker[ot] = res
        pairs.append((ref, res))

    # spot_gex.json
    gex_rows = _spot_gex_rows(refs, spot, sigma, trade_date)
    (day_dir / f"{ticker}_spot_gex.json").write_text(json.dumps({"data": gex_rows}))

    # option_contracts.json
    contract_rows = _option_contract_rows(refs, ohlcv_by_ticker, spot, sigma, trade_date)
    (day_dir / f"{ticker}_option_contracts.json").write_text(
        json.dumps({"data": contract_rows}))

    # net_prem_ticks.json
    prem_rows = _net_prem_rows(pairs, trade_date)
    (day_dir / f"{ticker}_net_prem_ticks.json").write_text(
        json.dumps({"data": prem_rows}))

    # darkpool.json — not available historically
    (day_dir / f"{ticker}_darkpool.json").write_text('{"data": []}')

    # technicals
    _write_technicals(day_dir, ticker, equity_ohlcv, trade_date)

    logger.info("%s %s: %d refs → %d with prices → %d contract rows",
                ticker, trade_date, len(refs), len(pairs), len(contract_rows))
    return pairs


def _write_empty_ticker(day_dir: Path, ticker: str) -> None:
    for fname in (f"{ticker}_spot_gex.json", f"{ticker}_option_contracts.json",
                  f"{ticker}_net_prem_ticks.json", f"{ticker}_darkpool.json"):
        (day_dir / fname).write_text('{"data": []}')


def _write_technicals(day_dir: Path, ticker: str, equity_ohlcv: list[dict],
                      trade_date: date) -> None:
    closes = [float(r["c"]) for r in equity_ohlcv if r.get("c")]
    rsi_rows = compute_rsi(closes)
    macd_rows = compute_macd(closes)
    # Stamp the last (most recent) row with the trade_date
    if rsi_rows:
        rsi_rows[-1]["timestamp"] = trade_date.isoformat()
    if macd_rows:
        macd_rows[-1]["timestamp"] = trade_date.isoformat()
    (day_dir / f"{ticker}_technicals_RSI.json").write_text(
        json.dumps({"data": rsi_rows}))
    (day_dir / f"{ticker}_technicals_MACD.json").write_text(
        json.dumps({"data": macd_rows}))


# ---------------------------------------------------------------------------
# Per-day orchestration
# ---------------------------------------------------------------------------

async def _fetch_day(
    session: aiohttp.ClientSession,
    trade_date: date,
    tickers: list[str],
    out_dir: Path,
    sem: asyncio.Semaphore,
) -> None:
    day_dir = out_dir / trade_date.isoformat()
    if (day_dir / "market_tide.json").exists():
        logger.info("%s already fetched — skipping", trade_date)
        return

    day_dir.mkdir(parents=True, exist_ok=True)
    logger.info("=== %s ===", trade_date)

    # VIX as IV proxy for the entire day
    sigma = await polygon_vix(session, trade_date, sem)
    logger.debug("%s VIX=%.1f%%", trade_date, sigma * 100)

    # Equity OHLCV lookback for all user tickers + SPY (for market tide)
    lookback_start = trade_date - timedelta(days=_OHLCV_LOOKBACK_DAYS)
    all_tickers = list(dict.fromkeys(tickers + ["SPY"]))
    equity_results = await asyncio.gather(
        *[polygon_equity_ohlcv(session, t, lookback_start, trade_date, sem)
          for t in all_tickers],
        return_exceptions=True,
    )
    equity_ohlcv: dict[str, list[dict]] = {}
    for t, res in zip(all_tickers, equity_results):
        if isinstance(res, Exception):
            logger.error("%s equity OHLCV error: %s", t, res)
            equity_ohlcv[t] = []
        else:
            equity_ohlcv[t] = res

    # flow_alerts.json — not available historically
    (day_dir / "flow_alerts.json").write_text('{"data": []}')

    # Per-ticker (sequential to stay within rate limit while option OHLCV is parallel inside)
    spy_pairs: list[tuple[dict, dict]] = []
    for ticker in tickers:
        pairs = await _process_ticker(
            session, ticker, trade_date, day_dir,
            equity_ohlcv.get(ticker, []), sigma, sem,
        )
        if ticker == "SPY":
            spy_pairs = pairs

    # If SPY not in user tickers, still build market tide from SPY equity only
    # (no options pairs → tide shows zeros which scores neutral in BlendScorer)
    if not spy_pairs and "SPY" not in tickers:
        spy_pairs = []

    tide_rows = _market_tide_row(spy_pairs, trade_date)
    (day_dir / "market_tide.json").write_text(json.dumps({"data": tide_rows}))
    logger.info("%s complete", trade_date)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _trading_days(start: date, end: date) -> list[date]:
    days: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon–Fri
            days.append(d)
        d += timedelta(days=1)
    return days


async def _main(args: argparse.Namespace) -> None:
    if not os.environ.get("POLYGON_API_KEY"):
        logger.error("POLYGON_API_KEY not set — add it to .env or export it")
        sys.exit(1)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    tickers = args.tickers or ["SPY"]

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    days = _trading_days(start, end)

    logger.info("Fetching %d trading days (%s → %s) for: %s",
                len(days), start, end, " ".join(tickers))
    logger.info("Output: %s", out_dir.resolve())

    sem = asyncio.Semaphore(_SEM)
    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        for i, d in enumerate(days):
            await _fetch_day(session, d, tickers, out_dir, sem)
            # Small inter-day pause to avoid burst spikes between dates
            if i < len(days) - 1:
                await asyncio.sleep(args.day_delay)

    logger.info("Done.  Run backtest:")
    logger.info("  python scripts/run_backtest.py --fixtures %s --start %s --end %s --tickers %s",
                out_dir, start, end, " ".join(tickers))


def main() -> None:
    p = argparse.ArgumentParser(
        description="Fetch Polygon.io historical options data for GEX backtest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--start", required=True, metavar="YYYY-MM-DD", help="First trading date")
    p.add_argument("--end", required=True, metavar="YYYY-MM-DD", help="Last trading date (inclusive)")
    p.add_argument("--tickers", nargs="+", metavar="TICKER",
                   help="Tickers to fetch (default: SPY)")
    p.add_argument("--out", default="data/history", metavar="DIR",
                   help="Output directory root (default: data/history)")
    p.add_argument("--day-delay", type=float, default=0.5, metavar="SEC",
                   help="Sleep between dates to avoid burst load (default: 0.5s)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
