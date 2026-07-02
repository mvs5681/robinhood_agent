#!/usr/bin/env python3
"""Launch the dashboard with realistic fake data — no live trading required.

Generates a telemetry.jsonl in /tmp, pre-populates ProposalStore with
a few dummy proposals, then serves the dashboard at http://localhost:8080/
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import tempfile
import webbrowser
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aiohttp import web

from trader.executor.schemas import ExecutionMode
from trader.gex.schemas import GEXRegime, GEXSetup, GEXWall
from trader.live.approval_server import create_app
from trader.live.proposals import ProposalStore
from trader.live.telemetry_reader import TelemetryReader
from trader.scoring.schemas import BlendScores, CandidateSignal
from trader.uw.schemas import FlowAlert, OptionContract

# ---------------------------------------------------------------------------
# Fake executor — approve button does nothing in demo mode
# ---------------------------------------------------------------------------

class _FakeExecutor:
    mode = ExecutionMode.RH_APPROVAL
    account_number = "DEMO"
    async def execute(self, candidate):
        class _R:
            placed = False
            order_id = None
            rejection_reason = "demo mode"
            review_summary = "Demo — no real order placed"
            class request:
                action = "buy"
                quantity = 1
                limit_price = Decimal("3.20")
        return _R()


# ---------------------------------------------------------------------------
# Telemetry event generator
# ---------------------------------------------------------------------------

STAGES = ["uw_fetch", "gex_setup", "blend_score", "flow_check",
          "contract_select", "risk_check", "order_attempt"]
TICKERS = ["AAPL", "SPY", "QQQ", "NVDA", "TSLA"]

def _ts(minutes_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.isoformat()

def _gen_events() -> list[dict]:
    rng = random.Random(42)
    events: list[dict] = []
    now = datetime.now(timezone.utc)

    # Funnel pass-through rates per stage
    pass_rates = {
        "uw_fetch": 1.0,
        "gex_setup": 0.85,
        "blend_score": 0.70,
        "flow_check": 0.45,
        "contract_select": 0.38,
        "risk_check": 0.28,
        "order_attempt": 0.20,
    }

    # Generate events across last 2 hours in batches (one batch per ~10 min)
    for batch in range(12):
        minutes_ago = (12 - batch) * 10 + rng.uniform(0, 5)
        ts = (now - timedelta(minutes=minutes_ago)).isoformat()

        active_tickers = rng.sample(TICKERS, rng.randint(2, 5))

        for ticker in active_tickers:
            active = True
            for stage in STAGES:
                if not active:
                    break
                roll = rng.random()
                if roll < pass_rates[stage] * 0.85:
                    result = "ok"
                elif roll < pass_rates[stage]:
                    result = "skipped"
                    active = False
                else:
                    result = "error" if rng.random() < 0.15 else "skipped"
                    active = False

                ev: dict = {
                    "timestamp": ts,
                    "stage": stage,
                    "ticker": ticker,
                    "result": result,
                    "duration_ms": round(rng.uniform(20, 400), 1),
                }
                if result == "skipped":
                    ev["reason"] = rng.choice([
                        "confidence below threshold",
                        "no flow confirmation",
                        "risk gate: delta exposure",
                        "spread too wide",
                        "no GEX structure",
                    ])

                # Stage-specific fields
                if stage == "gex_setup" and result == "ok":
                    ev.update({
                        "regime": rng.choice(["positive", "negative"]),
                        "direction": rng.choice(["call", "put"]),
                        "setup_type": rng.choice(["pin", "squeeze", "momentum"]),
                        "confidence": round(rng.uniform(0.4, 0.95), 3),
                        "flip_point": round(rng.uniform(190, 210), 1),
                        "target_level": round(rng.uniform(200, 215), 1),
                    })
                elif stage == "blend_score" and result == "ok":
                    scores = {k: round(rng.uniform(0.3, 0.95), 3)
                              for k in ["market_tide","darkpool","flow_pressure","iv_cost","technicals"]}
                    ev.update({
                        "composite": round(sum(scores.values()) / 5, 3),
                        "scores": scores,
                        "rank": rng.randint(1, 5),
                    })
                elif stage == "flow_check" and result == "ok":
                    ev.update({
                        "confirmed": True,
                        "direction": rng.choice(["call", "put"]),
                        "alert_premium": rng.randint(150_000, 2_000_000),
                    })
                elif stage == "contract_select" and result == "ok":
                    strike = rng.choice([195, 197.5, 200, 202.5, 205])
                    ev.update({
                        "selected": True,
                        "strike": strike,
                        "expiry": (date.today() + timedelta(days=rng.randint(7, 30))).isoformat(),
                        "delta": round(rng.uniform(0.30, 0.55), 3),
                        "dte": rng.randint(7, 30),
                        "spread_pct": round(rng.uniform(0.02, 0.12), 3),
                    })
                elif stage == "risk_check" and result == "ok":
                    ev.update({"approved": True, "rejection_reasons": []})
                elif stage == "order_attempt" and result == "ok":
                    ev.update({
                        "mode": "rh_approval",
                        "action": "buy",
                        "quantity": 1,
                        "limit_price": round(rng.uniform(1.5, 6.0), 2),
                        "placed": True,
                        "order_id": f"ord_{rng.randint(100000,999999)}",
                    })

                events.append(ev)

    # Add exit_signal P&L events
    for i in range(15):
        minutes_ago = rng.uniform(5, 115)
        ts = (now - timedelta(minutes=minutes_ago)).isoformat()
        pnl = round(rng.gauss(0.08, 0.25), 4)   # mean +8%, std 25%
        events.append({
            "timestamp": ts,
            "stage": "exit_signal",
            "ticker": rng.choice(TICKERS),
            "result": "ok",
            "duration_ms": round(rng.uniform(5, 30), 1),
            "position_id": f"pos_{rng.randint(1000,9999)}",
            "pnl_pct": pnl,
            "dte_remaining": rng.randint(0, 14),
            "entry_premium": round(rng.uniform(1.5, 5.0), 2),
            "current_premium": round(rng.uniform(1.0, 8.0), 2),
        })

    events.sort(key=lambda e: e["timestamp"])
    return events


# ---------------------------------------------------------------------------
# Fake proposals
# ---------------------------------------------------------------------------

def _make_setup(ticker: str, regime: str, direction: str, spot: float) -> GEXSetup:
    return GEXSetup(
        ticker=ticker,
        as_of=datetime.now(timezone.utc),
        spot_price=Decimal(str(spot)),
        regime=GEXRegime(regime),
        flip_point=Decimal(str(round(spot * 0.99, 2))),
        nearest_call_wall=GEXWall(
            strike=Decimal(str(round(spot * 1.02, 0))),
            net_gex=Decimal("1250000"),
            distance_pct=Decimal("0.02"),
            side="call_wall",
        ),
        nearest_put_wall=GEXWall(
            strike=Decimal(str(round(spot * 0.97, 0))),
            net_gex=Decimal("-850000"),
            distance_pct=Decimal("0.03"),
            side="put_wall",
        ),
        target_level=Decimal(str(round(spot * 1.025, 2))),
        candidate_direction=direction,  # type: ignore[arg-type]
        setup_type="squeeze",
        structure_confidence=0.78,
        raw_gex_by_strike=[],
    )


def _make_contract(ticker: str, direction: str, spot: float) -> OptionContract:
    strike = round(spot * 1.01 if direction == "call" else spot * 0.99, 0)
    expiry = date.today() + timedelta(days=14)
    mid = round(random.uniform(2.5, 5.5), 2)
    return OptionContract(
        ticker=ticker,
        expiry=expiry,
        strike=Decimal(str(strike)),
        type="call" if direction == "call" else "put",
        bid=Decimal(str(round(mid - 0.1, 2))),
        ask=Decimal(str(round(mid + 0.1, 2))),
        open_interest=random.randint(2000, 8000),
        volume=random.randint(300, 1200),
        implied_volatility=Decimal("0.32"),
        delta=Decimal("0.45"),
        gamma=Decimal("0.04"),
        theta=Decimal("-0.08"),
        vega=Decimal("0.15"),
    )


def _make_flow_alert(ticker: str, direction: str) -> FlowAlert:
    expiry = date.today() + timedelta(days=14)
    return FlowAlert(
        ticker=ticker,
        type="call" if direction == "call" else "put",
        strike=Decimal("200"),
        expiry=expiry,
        total_premium=Decimal("350000"),
        total_size=100,
        volume=250,
        open_interest=4500,
        alert_rule="unusual_volume",
        trade_count=12,
        created_at=datetime.now(timezone.utc),
    )


FAKE_PROPOSALS = [
    ("AAPL", "negative", "call", 195.50),
    ("SPY",  "positive", "put",  543.20),
    ("NVDA", "negative", "call", 118.75),
]

async def _populate_proposals(store: ProposalStore) -> None:
    rng = random.Random(7)
    for ticker, regime, direction, spot in FAKE_PROPOSALS:
        setup = _make_setup(ticker, regime, direction, spot)
        contract = _make_contract(ticker, direction, spot)
        alert = _make_flow_alert(ticker, direction)
        scores_raw = {k: round(rng.uniform(0.45, 0.92), 3)
                      for k in ["market_tide","darkpool","flow_pressure","iv_cost","technicals"]}
        candidate = CandidateSignal(
            ticker=ticker,
            as_of=datetime.now(timezone.utc),
            gex_setup=setup,
            blend_scores=BlendScores(
                **scores_raw,
                composite=round(sum(scores_raw.values()) / 5, 3),
            ),
            rank=1,
            flow_confirmed=True,
            flow_trigger=alert,
            selected_contract=contract,
            execution_status="proposed",
        )
        await store.add(candidate)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    # Write fake telemetry log
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, prefix="demo_telemetry_"
    )
    events = _gen_events()
    for ev in events:
        tmp.write(json.dumps(ev) + "\n")
    tmp.close()
    log_path = tmp.name
    print(f"Fake telemetry: {len(events)} events → {log_path}")

    proposal_store = ProposalStore()
    await _populate_proposals(proposal_store)
    print(f"Proposals: {len(FAKE_PROPOSALS)} pending proposals seeded")

    tel_reader = TelemetryReader(log_file=log_path)
    executor = _FakeExecutor()  # type: ignore[arg-type]

    app = create_app(
        proposal_store=proposal_store,
        executor=executor,  # type: ignore[arg-type]
        telemetry_reader=tel_reader,
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8080)
    await site.start()

    url = "http://localhost:8080/"
    print(f"\nDashboard → {url}")
    print("Press Ctrl+C to stop.\n")
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
