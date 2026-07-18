#!/usr/bin/env python3
"""Launch the dashboard with realistic fake data — no live trading required.

Generates fake telemetry events (with run_ids), 3 pending proposals with
full GEX/blend/contract data, and a fake GEX cache for the Market tab.
Serves at http://localhost:8080/
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
import tempfile
import webbrowser
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aiohttp import web

from trader.executor.schemas import ExecutionMode
from trader.exits.schemas import Position
from trader.gex.schemas import GEXRegime, GEXSetup, GEXWall
from trader.live.approval_server import create_app
from trader.live.cache import GEXCache, TickerSnapshot
from trader.live.position_store import PositionStore
from trader.live.proposals import ProposalStore
from trader.live.telemetry_reader import TelemetryReader
from trader.scoring.schemas import BlendScores, CandidateSignal
from trader.uw.schemas import FlowAlert, OptionContract


# ---------------------------------------------------------------------------
# Fake executor
# ---------------------------------------------------------------------------

class _FakeExecutor:
    mode = ExecutionMode.RH_APPROVAL
    account_number = "DEMO"

    async def execute(self, candidate):
        class _R:
            placed = False
            order_id = None
            rejection_reason = "demo mode — no real order placed"
            review_summary = "This is a demo run. In live mode the RH MCP would review and place this order."
            class request:
                action = "buy"
                quantity = 1
                limit_price = Decimal("3.20")
        return _R()


# ---------------------------------------------------------------------------
# Telemetry event generator (with run_ids)
# ---------------------------------------------------------------------------

TICKERS = ["AAPL", "SPY", "QQQ", "NVDA", "TSLA"]
DECISION_STAGES = ["gex_setup", "blend_score", "flow_check",
                   "contract_select", "risk_check", "order_attempt"]

SKIP_REASONS = {
    "gex_setup":       "confidence below threshold (0.12 < 0.15)",
    "blend_score":     "composite score below minimum (0.42 < 0.55)",
    "flow_check":      "no whale print in last 4 hours above $100K",
    "contract_select": "no liquid contract: spread 18% > max 15%",
    "risk_check":      "max concurrent positions reached (3/3)",
}


def _gen_events() -> list[dict]:
    rng = random.Random(42)
    now = datetime.now(timezone.utc)
    events: list[dict] = []

    pass_rates = {
        "gex_setup": 0.80, "blend_score": 0.68, "flow_check": 0.45,
        "contract_select": 0.38, "risk_check": 0.28, "order_attempt": 0.22,
    }

    # 12 batches across last 2 hours — each batch is one scanner cycle
    for batch in range(12):
        minutes_ago = (12 - batch) * 10 + rng.uniform(0, 4)
        ts = (now - timedelta(minutes=minutes_ago)).isoformat()
        active_tickers = rng.sample(TICKERS, rng.randint(2, 5))

        for ticker in active_tickers:
            run_id = f"{ticker}:{uuid4().hex[:8]}"
            stop = False
            spot = rng.uniform(150, 550)
            regime = rng.choice(["positive", "negative", "negative"])  # bias negative
            direction = rng.choice(["call", "put"])
            setup_type = rng.choice(["squeeze", "squeeze", "pin", "momentum"])
            confidence = round(rng.uniform(0.25, 0.95), 3)
            flip = round(spot * rng.uniform(0.97, 1.00), 1)
            call_wall = round(spot * rng.uniform(1.01, 1.04), 1)
            put_wall = round(spot * rng.uniform(0.95, 0.99), 1)
            target = round(call_wall if direction == "call" else put_wall, 1)
            scores = {k: round(rng.uniform(0.3, 0.95), 3)
                      for k in ["market_tide", "darkpool", "flow_pressure", "iv_cost", "technicals"]}
            composite = round(sum(scores.values()) / 5, 3)
            strike = round(spot * rng.uniform(0.99, 1.02), 0)

            # uw_fetch (no run_id — market-wide)
            events.append({
                "timestamp": ts, "stage": "uw_fetch", "ticker": ticker,
                "result": "ok", "duration_ms": round(rng.uniform(80, 350), 1),
                "endpoint": "get_spot_exposures_by_strike", "record_count": rng.randint(30, 80),
            })

            for stage in DECISION_STAGES:
                if stop:
                    break
                roll = rng.random()
                if roll < pass_rates.get(stage, 1.0) * 0.80:
                    result = "ok"
                elif roll < pass_rates.get(stage, 1.0):
                    result = "skipped"
                    stop = True
                else:
                    result = "error" if rng.random() < 0.12 else "skipped"
                    stop = True

                ev: dict = {
                    "timestamp": ts, "stage": stage, "ticker": ticker,
                    "result": result, "run_id": run_id,
                    "duration_ms": round(rng.uniform(10, 200), 1),
                }
                if result == "skipped":
                    ev["reason"] = SKIP_REASONS.get(stage, "skipped")

                if stage == "gex_setup" and result == "ok":
                    ev.update({
                        "regime": regime, "direction": direction, "setup_type": setup_type,
                        "confidence": confidence, "flip_point": flip, "target_level": target,
                        "call_wall": call_wall, "put_wall": put_wall, "spot_price": round(spot, 2),
                    })
                elif stage == "blend_score" and result == "ok":
                    ev.update({
                        "composite": composite, "scores": scores, "rank": rng.randint(1, 5),
                    })
                elif stage == "flow_check" and result == "ok":
                    ev.update({
                        "confirmed": True, "direction": direction,
                        "alert_premium": rng.randint(120_000, 2_500_000),
                    })
                elif stage == "contract_select" and result == "ok":
                    ev.update({
                        "selected": True, "strike": strike,
                        "expiry": (date.today() + timedelta(days=rng.randint(7, 30))).isoformat(),
                        "delta": round(rng.uniform(0.30, 0.55), 3),
                        "dte": rng.randint(7, 30),
                        "spread_pct": round(rng.uniform(0.02, 0.12), 3),
                    })
                elif stage == "risk_check" and result == "ok":
                    ev.update({"approved": True, "rejection_reasons": []})
                elif stage == "order_attempt" and result == "ok":
                    ev.update({
                        "mode": "rh_approval", "action": "buy", "quantity": 1,
                        "limit_price": round(rng.uniform(1.5, 7.0), 2),
                        "placed": True, "order_id": f"ord_{rng.randint(100000, 999999)}",
                        "review_summary": "Order reviewed and approved. Strike is liquid, spread within limits.",
                    })

                events.append(ev)

    # Exit signal P&L history
    for _ in range(18):
        minutes_ago = rng.uniform(5, 115)
        ts = (now - timedelta(minutes=minutes_ago)).isoformat()
        pnl = round(rng.gauss(0.09, 0.28), 4)
        exit_reason = rng.choice(["profit_target", "profit_target", "stop_loss", "dte_decay"])
        events.append({
            "timestamp": ts, "stage": "exit_signal", "ticker": rng.choice(TICKERS),
            "result": "ok", "duration_ms": round(rng.uniform(5, 30), 1),
            "position_id": f"pos_{rng.randint(1000, 9999)}",
            "pnl_pct": pnl, "reason": exit_reason,
            "dte_remaining": rng.randint(0, 14),
            "entry_premium": round(rng.uniform(1.5, 5.0), 2),
            "current_premium": round(rng.uniform(0.8, 9.0), 2),
        })

    events.sort(key=lambda e: e["timestamp"])
    return events


# ---------------------------------------------------------------------------
# Fake proposals + cache
# ---------------------------------------------------------------------------

FAKE_POSITIONS = [
    ("AAPL", "negative", "call", 195.50),
    ("SPY",  "positive", "put",  543.20),
    ("NVDA", "negative", "call", 118.75),
]


def _make_setup(ticker: str, regime: str, direction: str, spot: float) -> GEXSetup:
    call_w = round(spot * 1.022, 2)
    put_w  = round(spot * 0.973, 2)
    return GEXSetup(
        ticker=ticker,
        as_of=datetime.now(timezone.utc),
        spot_price=Decimal(str(spot)),
        regime=GEXRegime(regime),
        flip_point=Decimal(str(round(spot * 0.991, 2))),
        nearest_call_wall=GEXWall(
            strike=Decimal(str(call_w)), net_gex=Decimal("1250000"),
            distance_pct=Decimal("0.022"), side="call_wall",
        ),
        nearest_put_wall=GEXWall(
            strike=Decimal(str(put_w)), net_gex=Decimal("-850000"),
            distance_pct=Decimal("0.027"), side="put_wall",
        ),
        target_level=Decimal(str(call_w if direction == "call" else put_w)),
        candidate_direction=direction,  # type: ignore[arg-type]
        setup_type="squeeze",
        structure_confidence=round(random.uniform(0.65, 0.88), 3),
        raw_gex_by_strike=[],
    )


def _make_contract(ticker: str, direction: str, spot: float) -> OptionContract:
    strike = round(spot * (1.012 if direction == "call" else 0.988), 0)
    expiry = date.today() + timedelta(days=14)
    mid = round(random.uniform(2.5, 5.5), 2)
    return OptionContract(
        ticker=ticker, expiry=expiry, strike=Decimal(str(strike)),
        type="call" if direction == "call" else "put",
        bid=Decimal(str(round(mid - 0.12, 2))),
        ask=Decimal(str(round(mid + 0.12, 2))),
        open_interest=random.randint(2000, 9000),
        volume=random.randint(400, 1500),
        implied_volatility=Decimal("0.32"),
        delta=Decimal("0.46"), gamma=Decimal("0.04"),
        theta=Decimal("-0.09"), vega=Decimal("0.14"),
    )


def _make_alert(ticker: str, direction: str, spot: float) -> FlowAlert:
    expiry = date.today() + timedelta(days=14)
    return FlowAlert(
        ticker=ticker, type="call" if direction == "call" else "put",
        strike=Decimal(str(round(spot * 1.01, 0))), expiry=expiry,
        total_premium=Decimal(str(random.randint(200_000, 1_500_000))),
        total_size=random.randint(50, 300), volume=random.randint(200, 800),
        open_interest=random.randint(3000, 8000),
        alert_rule="unusual_volume_sweep", trade_count=random.randint(5, 20),
        has_sweep=random.random() > 0.4, has_floor=random.random() > 0.6,
        created_at=datetime.now(timezone.utc),
    )


async def _populate_proposals(store: ProposalStore) -> None:
    rng = random.Random(7)
    for ticker, regime, direction, spot in FAKE_POSITIONS:
        setup = _make_setup(ticker, regime, direction, spot)
        contract = _make_contract(ticker, direction, spot)
        alert = _make_alert(ticker, direction, spot)
        scores_raw = {k: round(rng.uniform(0.48, 0.92), 3)
                      for k in ["market_tide", "darkpool", "flow_pressure", "iv_cost", "technicals"]}
        candidate = CandidateSignal(
            ticker=ticker, as_of=datetime.now(timezone.utc),
            gex_setup=setup,
            blend_scores=BlendScores(**scores_raw, composite=round(sum(scores_raw.values()) / 5, 3)),
            rank=1, flow_confirmed=True, flow_trigger=alert,
            selected_contract=contract, execution_status="proposed",
        )
        run_id = f"{ticker}:{uuid4().hex[:8]}"
        await store.add(candidate, run_id=run_id)


async def _populate_positions(store: PositionStore) -> None:
    """Add fake open positions to the PositionStore for the Purchased phase."""
    rng = random.Random(13)
    for ticker, regime, direction, spot in FAKE_POSITIONS:
        contract = _make_contract(ticker, direction, spot)
        setup = _make_setup(ticker, regime, direction, spot)
        entry = Decimal(str(round(rng.uniform(2.0, 6.0), 2)))
        pos = Position(
            position_id=f"pos_{ticker}_{rng.randint(10000, 99999)}",
            ticker=ticker,
            contract=contract,
            entry_premium=entry,
            target_level=setup.target_level,
            opened_at=datetime.now(timezone.utc) - timedelta(minutes=rng.randint(30, 180)),
            quantity=1,
        )
        await store.add(pos)


async def _build_fake_cache() -> GEXCache:
    """Populate the GEX cache with fake ticker snapshots for the Market tab."""
    cache = GEXCache()
    snapshots = {}
    all_tickers = ["AAPL", "SPY", "QQQ", "NVDA", "TSLA", "MSFT", "META"]
    configs = [
        ("negative", "call", 195.5),
        ("positive", "put",  543.2),
        ("negative", "call", 118.75),
        ("positive", "call", 420.10),
        ("negative", "put",  182.30),
        ("mixed",    "none", 385.60),
        ("negative", "call", 490.20),
    ]
    for ticker, (regime, direction, spot) in zip(all_tickers, configs):
        snap = TickerSnapshot()
        snap.gex_setup = _make_setup(ticker, regime, direction, spot)
        snapshots[ticker] = snap
    await cache.update([], snapshots)
    return cache


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    # Write fake telemetry
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, prefix="demo_tel_")
    events = _gen_events()
    for ev in events:
        tmp.write(json.dumps(ev) + "\n")
    tmp.close()
    log_path = tmp.name
    print(f"Fake telemetry: {len(events)} events → {log_path}")

    proposal_store = ProposalStore()
    await _populate_proposals(proposal_store)
    print(f"Proposals: {len(FAKE_POSITIONS)} pending")

    position_store = PositionStore()
    await _populate_positions(position_store)
    print(f"Positions: {position_store.count} open")

    cache = await _build_fake_cache()
    print(f"Market cache: {len(cache.tickers)} tickers")

    tel_reader = TelemetryReader(log_file=log_path)
    executor = _FakeExecutor()  # type: ignore[arg-type]

    app = create_app(
        proposal_store=proposal_store,
        executor=executor,  # type: ignore[arg-type]
        telemetry_reader=tel_reader,
        cache=cache,
        position_store=position_store,
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8080)
    await site.start()

    url = "http://localhost:8080/"
    print(f"\nDashboard → {url}\nPress Ctrl+C to stop.\n")
    webbrowser.open(url)

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.cleanup()
        Path(log_path).unlink(missing_ok=True)
        print("Stopped.")


if __name__ == "__main__":
    asyncio.run(main())
