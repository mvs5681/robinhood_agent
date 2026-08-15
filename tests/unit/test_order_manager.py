"""Tests for OrderLifecycleManager — fill promotion, repricing, give-up."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from trader.executor.schemas import ExecutionMode, OrderRequest, OrderResult
from trader.gex.schemas import GEXRegime, GEXSetup
from trader.live.order_manager import OrderLifecycleManager, WorkingOrder
from trader.live.position_store import PositionStore
from trader.scoring.schemas import BlendScores, CandidateSignal
from trader.uw.schemas import OptionContract

ACCOUNT = "869536151"
ORDER_ID = "6a57a2c5-ba56-40c3-9624-d0862beda75d"
OPTION_ID = "186ed0b7-49be-4328-bb89-26ab5596fd17"


def _contract() -> OptionContract:
    return OptionContract(
        ticker="HOOD", expiry=date(2026, 8, 14), strike=Decimal("130"),
        type="call", bid=Decimal("4.10"), ask=Decimal("4.30"),
        open_interest=1000, volume=500, delta=Decimal("0.40"),
    )


def _candidate() -> CandidateSignal:
    setup = GEXSetup(
        ticker="HOOD", as_of=datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
        spot_price=Decimal("120"), regime=GEXRegime.POSITIVE, flip_point=None,
        nearest_call_wall=None, nearest_put_wall=None,
        target_level=Decimal("140"), candidate_direction="call",
        setup_type="pin", structure_confidence=0.6, raw_gex_by_strike=[],
    )
    return CandidateSignal(
        ticker="HOOD", as_of=setup.as_of, gex_setup=setup,
        blend_scores=BlendScores(market_tide=0.5, darkpool=0.5, flow_pressure=0.5,
                                 iv_cost=0.5, technicals=0.5, composite=0.5),
        execution_status="proposed", selected_contract=_contract(),
    )


def _result(order_id: str = ORDER_ID, quantity: int = 1) -> OrderResult:
    request = OrderRequest(
        candidate=_candidate(), action="buy_to_open", quantity=quantity,
        limit_price=Decimal("4.20"), mode=ExecutionMode.RH_APPROVAL,
    )
    return OrderResult(request=request, placed=True, order_id=order_id,
                       timestamp=datetime.now(timezone.utc))


def _order(state: str, *, order_id: str = ORDER_ID, processed: str = "0.00000",
           processed_premium: str = "0", quantity: str = "1.00000") -> dict:
    return {"data": {"orders": [{
        "id": order_id, "chain_symbol": "HOOD", "state": state,
        "quantity": quantity, "processed_quantity": processed,
        "processed_premium": processed_premium, "price": "4.20000000",
        "legs": [{"option_id": OPTION_ID, "side": "buy", "position_effect": "open",
                  "expiration_date": "2026-08-14", "strike_price": "130.0000",
                  "option_type": "call"}],
    }]}}


def _quote(bid: str = "4.30", ask: str = "4.50", mark: str = "4.40") -> dict:
    return {"data": {"results": [{"quote": {
        "instrument_id": OPTION_ID, "bid_price": bid, "ask_price": ask,
        "mark_price": mark,
    }}]}}


def _tool(response) -> MagicMock:
    t = MagicMock()
    t.ainvoke = AsyncMock(return_value=response)
    return t


def _tool_by_state(orders_by_state: dict) -> MagicMock:
    """get_option_orders mock that returns a different fixture per state=
    param, and an empty result for any state not in the map."""
    async def _invoke(kwargs):
        return orders_by_state.get(kwargs.get("state"), {"data": {"orders": []}})
    t = MagicMock()
    t.ainvoke = AsyncMock(side_effect=_invoke)
    return t


def _tool_by_state_sequence(sequences_by_state: dict) -> MagicMock:
    """get_option_orders mock where each state= yields the next response from
    its own list on successive calls (repeating the last once exhausted) —
    used to simulate a sweep that comes back empty once, then finds orders."""
    calls: dict[str, int] = {}

    async def _invoke(kwargs):
        state = kwargs.get("state")
        seq = sequences_by_state.get(state)
        if not seq:
            return {"data": {"orders": []}}
        idx = calls.get(state, 0)
        calls[state] = idx + 1
        return seq[min(idx, len(seq) - 1)]

    t = MagicMock()
    t.ainvoke = AsyncMock(side_effect=_invoke)
    return t


def _manager(rh: dict, store: PositionStore | None = None) -> OrderLifecycleManager:
    return OrderLifecycleManager(
        rh_tools=rh,
        position_store=store or PositionStore(),
        account_number=ACCOUNT,
    )


class TestTrack:
    async def test_track_registers_working_order(self):
        mgr = _manager({})
        await mgr.track(_candidate(), _result())
        assert mgr.working_count == 1

    async def test_unplaced_result_not_tracked(self):
        mgr = _manager({})
        r = _result()
        r = r.model_copy(update={"placed": False, "order_id": None})
        await mgr.track(_candidate(), r)
        assert mgr.working_count == 0


class TestFillPromotion:
    async def test_fill_promotes_to_position_with_avg_fill_premium(self):
        store = PositionStore()
        rh = {"get_option_orders": _tool(
            _order("filled", processed="1.00000", processed_premium="415.00")
        )}
        mgr = _manager(rh, store)
        await mgr.track(_candidate(), _result())
        await mgr._tick()

        assert mgr.working_count == 0
        positions = await store.all()
        assert len(positions) == 1
        pos = positions[0]
        assert pos.ticker == "HOOD"
        assert pos.entry_premium == Decimal("4.1500")   # 415 / (1 * 100)
        assert pos.quantity == 1
        assert pos.option_instrument_id == OPTION_ID
        assert pos.target_level == Decimal("140")
        # Carried forward from the candidate's GEXSetup for backtest-vs-reality
        # comparison — see _candidate()'s setup (regime=POSITIVE, setup_type="pin")
        assert pos.entry_regime == "positive"
        assert pos.entry_setup_type == "pin"

    async def test_partial_fill_then_cancel_promotes_partial(self):
        store = PositionStore()
        rh = {"get_option_orders": _tool(
            _order("cancelled", processed="1.00000", processed_premium="420.00",
                   quantity="2.00000")
        )}
        mgr = _manager(rh, store)
        await mgr.track(_candidate(), _result(quantity=2))
        await mgr._tick()

        positions = await store.all()
        assert len(positions) == 1
        assert positions[0].quantity == 1


class TestReprice:
    async def test_stale_order_gets_cancel_requested(self):
        rh = {
            "get_option_orders": _tool(_order("confirmed")),
            "cancel_option_order": _tool({"data": {}}),
        }
        mgr = _manager(rh)
        await mgr.track(_candidate(), _result())
        wo = next(iter(mgr._orders.values()))
        wo.placed_at = wo.last_action_at = datetime.now(timezone.utc) - timedelta(seconds=200)
        await mgr._tick()

        rh["cancel_option_order"].ainvoke.assert_called_once()
        assert wo.cancelling is True
        assert not wo.giving_up

    async def test_cancelled_order_replaced_at_laddered_price(self):
        new_order = {"data": {"order": {"id": "new-order-id"}}}
        rh = {
            "get_option_orders": _tool(_order("cancelled")),
            "get_option_quotes": _tool(_quote(mark="4.40")),
            "place_option_order": _tool(new_order),
        }
        mgr = _manager(rh)
        await mgr.track(_candidate(), _result())
        wo = next(iter(mgr._orders.values()))
        wo.cancelling = True
        wo.option_id = OPTION_ID
        await mgr._tick()

        place_params = rh["place_option_order"].ainvoke.call_args[0][0]
        assert place_params["price"] == "4.40"          # attempt 0 → fresh mid
        assert place_params["legs"][0]["option_id"] == OPTION_ID
        assert "ref_id" in place_params
        assert wo.order_id == "new-order-id"
        assert wo.attempts == 1
        assert wo.cancelling is False

    async def test_second_replacement_steps_toward_ask(self):
        rh = {
            "get_option_orders": _tool(_order("cancelled")),
            "get_option_quotes": _tool(_quote(mark="4.40", ask="4.60")),
            "place_option_order": _tool({"data": {"order": {"id": "n2"}}}),
        }
        mgr = _manager(rh)
        await mgr.track(_candidate(), _result())
        wo = next(iter(mgr._orders.values()))
        wo.cancelling = True
        wo.option_id = OPTION_ID
        wo.attempts = 1
        await mgr._tick()
        place_params = rh["place_option_order"].ainvoke.call_args[0][0]
        assert place_params["price"] == "4.50"          # (mid + ask) / 2

    async def test_replacement_price_capped_at_risk_premium(self):
        rh = {
            "get_option_orders": _tool(_order("cancelled")),
            "get_option_quotes": _tool(_quote(mark="6.50", ask="7.00")),
            "place_option_order": _tool({"data": {"order": {"id": "n3"}}}),
        }
        mgr = _manager(rh)   # cap = 500/100 = 5.00
        await mgr.track(_candidate(), _result())
        wo = next(iter(mgr._orders.values()))
        wo.cancelling = True
        wo.option_id = OPTION_ID
        await mgr._tick()
        place_params = rh["place_option_order"].ainvoke.call_args[0][0]
        assert place_params["price"] == "5.00"


class TestGiveUp:
    async def test_past_give_up_age_cancels_finally(self):
        rh = {
            "get_option_orders": _tool(_order("confirmed")),
            "cancel_option_order": _tool({"data": {}}),
        }
        mgr = _manager(rh)
        await mgr.track(_candidate(), _result())
        wo = next(iter(mgr._orders.values()))
        wo.placed_at = wo.last_action_at = datetime.now(timezone.utc) - timedelta(seconds=700)
        await mgr._tick()
        assert wo.giving_up is True
        assert wo.cancelling is True

    async def test_giving_up_cancelled_order_not_replaced(self):
        store = PositionStore()
        rh = {
            "get_option_orders": _tool(_order("cancelled")),
            "place_option_order": _tool({"data": {"order": {"id": "nope"}}}),
        }
        mgr = _manager(rh, store)
        await mgr.track(_candidate(), _result())
        wo = next(iter(mgr._orders.values()))
        wo.cancelling = True
        wo.giving_up = True
        await mgr._tick()

        rh["place_option_order"].ainvoke.assert_not_called()
        assert mgr.working_count == 0
        assert await store.all() == []


class TestAdoption:
    @pytest.fixture(autouse=True)
    def _no_real_sleep(self, monkeypatch):
        # A zero-adoption sweep now retries once after asyncio.sleep(3) —
        # patch it out so tests asserting adopted == 0 don't actually wait.
        from trader.live import order_manager
        monkeypatch.setattr(order_manager.asyncio, "sleep", AsyncMock())

    async def test_adopts_working_buy_order_from_live_shape(self):
        rh = {"get_option_orders": _tool(_order("confirmed"))}
        mgr = _manager(rh)
        adopted = await mgr.adopt_working_orders()
        assert adopted >= 1
        wo = next(iter(mgr._orders.values()))
        assert wo.ticker == "HOOD"
        assert wo.option_id == OPTION_ID
        assert wo.candidate is None
        assert wo.price == Decimal("4.20000000")

    async def test_does_not_adopt_sell_orders(self):
        order = _order("confirmed")
        order["data"]["orders"][0]["legs"][0]["side"] = "sell"
        rh = {"get_option_orders": _tool(order)}
        mgr = _manager(rh)
        assert await mgr.adopt_working_orders() == 0

    async def test_sweeps_all_four_adoptable_states(self):
        # A different order per state — proves the sweep actually queries
        # partially_filled and pending_cancelled, not just confirmed/queued
        # (missing those left contracts that already cost real money
        # completely unmonitored after a restart).
        rh = {"get_option_orders": _tool_by_state({
            "queued": _order("queued", order_id="o-queued"),
            "confirmed": _order("confirmed", order_id="o-confirmed"),
            "partially_filled": _order("partially_filled", order_id="o-partial",
                                       processed="0.00000"),
            "pending_cancelled": _order("pending_cancelled", order_id="o-pending-cancel"),
        })}
        mgr = _manager(rh)
        adopted = await mgr.adopt_working_orders()
        assert adopted == 4
        assert {"o-queued", "o-confirmed", "o-partial", "o-pending-cancel"} == set(mgr._orders)

    async def test_partial_fill_adopted_as_immediate_position_plus_remainder(self):
        store = PositionStore()
        rh = {"get_option_orders": _tool_by_state({
            "partially_filled": _order(
                "partially_filled", order_id="o-partial",
                quantity="3.00000", processed="1.00000", processed_premium="415.00",
            ),
        })}
        mgr = _manager(rh, store)
        adopted = await mgr.adopt_working_orders()
        assert adopted == 1

        # filled contract is protected immediately, not left for later
        positions = await store.all()
        assert len(positions) == 1
        assert positions[0].quantity == 1
        assert positions[0].entry_premium == Decimal("4.1500")
        # No CandidateSignal for an adopted order — regime/setup_type unknown
        assert positions[0].entry_regime is None
        assert positions[0].entry_setup_type is None

        # remaining 2 contracts still being chased as a working order
        assert mgr.working_count == 1
        wo = next(iter(mgr._orders.values()))
        assert wo.quantity == 2
        assert wo.cancelling is False

    async def test_fully_processed_partial_fill_tracks_no_working_order(self):
        # processed == quantity: nothing left pending, only the Position matters
        store = PositionStore()
        rh = {"get_option_orders": _tool_by_state({
            "partially_filled": _order(
                "partially_filled", order_id="o-done",
                quantity="1.00000", processed="1.00000", processed_premium="420.00",
            ),
        })}
        mgr = _manager(rh, store)
        await mgr.adopt_working_orders()
        assert len(await store.all()) == 1
        assert mgr.working_count == 0

    async def test_pending_cancelled_adopted_without_chasing_a_replacement(self):
        rh = {"get_option_orders": _tool_by_state({
            "pending_cancelled": _order("pending_cancelled", order_id="o-cancel"),
        })}
        mgr = _manager(rh)
        await mgr.adopt_working_orders()
        wo = mgr._orders["o-cancel"]
        assert wo.cancelling is True
        assert wo.giving_up is True

    async def test_adoption_is_idempotent_across_repeated_state_queries(self):
        # Regression against the old 2-state sweep's dedup: the same order
        # id must not be double-counted even though every state query in
        # this fixture returns it.
        rh = {"get_option_orders": _tool(_order("confirmed"))}
        mgr = _manager(rh)
        adopted = await mgr.adopt_working_orders()
        assert adopted == 1
        assert mgr.working_count == 1


class TestAdoptionRetryOnEmpty:
    """Same silent-empty-result class as the pre-fix reconciler bug: a false
    "0 adopted" from a parse miss must not look identical to genuinely
    having nothing to adopt — see order_manager.py's adopt_working_orders."""

    @pytest.fixture(autouse=True)
    def _no_real_sleep(self, monkeypatch):
        from trader.live import order_manager
        monkeypatch.setattr(order_manager.asyncio, "sleep", AsyncMock())

    async def test_retries_once_when_first_sweep_adopts_nothing(self):
        rh = {"get_option_orders": _tool_by_state_sequence({
            "confirmed": [{"data": {"orders": []}}, _order("confirmed")],
        })}
        mgr = _manager(rh)
        adopted = await mgr.adopt_working_orders()

        assert adopted == 1
        # 4 states x 2 full sweeps (empty first, real second)
        assert rh["get_option_orders"].ainvoke.await_count == 8

    async def test_does_not_retry_when_first_sweep_already_adopts(self):
        rh = {"get_option_orders": _tool_by_state({"confirmed": _order("confirmed")})}
        mgr = _manager(rh)
        adopted = await mgr.adopt_working_orders()

        assert adopted == 1
        assert rh["get_option_orders"].ainvoke.await_count == 4  # one sweep, no retry

    async def test_logs_raw_response_when_still_empty_after_retry(self, caplog):
        rh = {"get_option_orders": _tool(_order("confirmed", processed="0"))}
        # sell orders are never adopted — genuinely empty every time
        order = _order("confirmed")
        order["data"]["orders"][0]["legs"][0]["side"] = "sell"
        rh = {"get_option_orders": _tool(order)}
        mgr = _manager(rh)

        with caplog.at_level("INFO", logger="trader.live.order_manager"):
            adopted = await mgr.adopt_working_orders()

        assert adopted == 0
        assert rh["get_option_orders"].ainvoke.await_count == 8  # retried once, then stopped
        assert any("raw responses" in r.message for r in caplog.records)
