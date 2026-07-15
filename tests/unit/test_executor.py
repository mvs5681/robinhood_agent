"""Unit tests for Phase 7: Executor."""

from datetime import datetime, date, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trader.executor.executor import Executor, _extract_order_id, _get_blocking_alerts, _summarize_review
from trader.executor.schemas import ExecutionMode, OrderRequest, OrderResult
from trader.gex.schemas import GEXRegime, GEXSetup
from trader.scoring.schemas import BlendScores, CandidateSignal
from trader.uw.schemas import OptionContract


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

AS_OF = datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc)
FAKE_OPTION_ID = "abc-123-uuid"
FAKE_ORDER_ID = "order-456-uuid"
ACCOUNT = "TEST123456"


def _make_contract(mid: str = "3.00") -> OptionContract:
    bid = Decimal(mid) - Decimal("0.05")
    ask = Decimal(mid) + Decimal("0.05")
    return OptionContract(
        ticker="AAPL",
        expiry=date(2026, 7, 25),
        strike=Decimal("200"),
        type="call",
        bid=bid,
        ask=ask,
        open_interest=8000,
        volume=2000,
        delta=Decimal("0.38"),
    )


def _make_setup() -> GEXSetup:
    return GEXSetup(
        ticker="AAPL",
        as_of=AS_OF,
        spot_price=Decimal("192"),
        regime=GEXRegime.POSITIVE,
        flip_point=None,
        nearest_call_wall=None,
        nearest_put_wall=None,
        target_level=Decimal("200"),
        candidate_direction="call",
        setup_type="pin",
        structure_confidence=0.80,
        raw_gex_by_strike=[],
    )


def _make_candidate(status: str = "proposed", with_contract: bool = True) -> CandidateSignal:
    return CandidateSignal(
        ticker="AAPL",
        as_of=AS_OF,
        gex_setup=_make_setup(),
        blend_scores=BlendScores(
            market_tide=0.7,
            darkpool=0.8,
            flow_pressure=0.7,
            iv_cost=0.6,
            technicals=0.75,
            composite=0.71,
        ),
        execution_status=status,
        selected_contract=_make_contract() if with_contract else None,
    )


def _mock_tool(name: str, return_value) -> MagicMock:
    t = MagicMock()
    t.name = name
    t.ainvoke = AsyncMock(return_value=return_value)
    return t


def _rh_tools(
    instruments_response=None,
    review_response=None,
    place_response=None,
) -> dict:
    instruments_response = instruments_response or {"results": [{"id": FAKE_OPTION_ID}]}
    review_response = review_response or {"order_checks": [], "quote": {"mid_price": "3.00"}}
    place_response = place_response or {"id": FAKE_ORDER_ID}
    return {
        "get_option_instruments": _mock_tool("get_option_instruments", instruments_response),
        "review_option_order": _mock_tool("review_option_order", review_response),
        "place_option_order": _mock_tool("place_option_order", place_response),
    }


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_extract_order_id_from_id_key(self):
        assert _extract_order_id({"id": "abc"}) == "abc"

    def test_extract_order_id_from_order_id_key(self):
        assert _extract_order_id({"order_id": "xyz"}) == "xyz"

    def test_extract_order_id_non_dict(self):
        assert _extract_order_id("string") is None
        assert _extract_order_id(None) is None

    def test_summarize_review_empty_checks(self):
        result = _summarize_review({"order_checks": [], "quote": {"mid_price": "2.50"}})
        assert "mid=$2.50" in result

    def test_summarize_review_with_checks(self):
        review = {"order_checks": [{"detail": "Margin required: $500"}]}
        result = _summarize_review(review)
        assert "Margin required" in result

    def test_summarize_review_non_dict(self):
        result = _summarize_review("raw string")
        assert result == "raw string"

    def test_get_blocking_alerts_fatal(self):
        review = {"order_checks": [
            {"severity": "fatal", "detail": "Account not authorized"},
            {"severity": "warning", "detail": "Low liquidity"},
        ]}
        alerts = _get_blocking_alerts(review)
        assert alerts == ["Account not authorized"]

    def test_get_blocking_alerts_error(self):
        review = {"order_checks": [{"severity": "error", "detail": "Insufficient buying power"}]}
        assert _get_blocking_alerts(review) == ["Insufficient buying power"]

    def test_get_blocking_alerts_empty(self):
        assert _get_blocking_alerts({"order_checks": []}) == []

    def test_get_blocking_alerts_non_dict(self):
        assert _get_blocking_alerts("not a dict") == []


# ---------------------------------------------------------------------------
# propose_only mode
# ---------------------------------------------------------------------------


class TestProposeOnly:
    async def test_returns_placed_false(self):
        executor = Executor(mode=ExecutionMode.PROPOSE_ONLY, account_number=ACCOUNT)
        result = await executor.execute(_make_candidate())
        assert isinstance(result, OrderResult)
        assert result.placed is False

    async def test_no_rh_tools_called(self):
        rh = _rh_tools()
        executor = Executor(
            mode=ExecutionMode.PROPOSE_ONLY,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        await executor.execute(_make_candidate())
        rh["get_option_instruments"].ainvoke.assert_not_called()
        rh["review_option_order"].ainvoke.assert_not_called()
        rh["place_option_order"].ainvoke.assert_not_called()

    async def test_order_id_is_none(self):
        executor = Executor(mode=ExecutionMode.PROPOSE_ONLY, account_number=ACCOUNT)
        result = await executor.execute(_make_candidate())
        assert result.order_id is None

    async def test_request_carries_candidate(self):
        executor = Executor(mode=ExecutionMode.PROPOSE_ONLY, account_number=ACCOUNT)
        candidate = _make_candidate()
        result = await executor.execute(candidate)
        assert result.request.candidate.ticker == "AAPL"
        assert result.request.action == "buy_to_open"
        assert result.request.mode == ExecutionMode.PROPOSE_ONLY

    async def test_limit_price_set_to_contract_mid(self):
        executor = Executor(mode=ExecutionMode.PROPOSE_ONLY, account_number=ACCOUNT)
        result = await executor.execute(_make_candidate())
        contract = _make_candidate().selected_contract
        assert result.request.limit_price == contract.mid

    async def test_raises_if_no_selected_contract(self):
        executor = Executor(mode=ExecutionMode.PROPOSE_ONLY, account_number=ACCOUNT)
        with pytest.raises(ValueError, match="selected_contract is None"):
            await executor.execute(_make_candidate(with_contract=False))


# ---------------------------------------------------------------------------
# Long-only constraint
# ---------------------------------------------------------------------------


class TestLongOnlyConstraint:
    def test_check_order_type_rejects_sell_to_open(self):
        executor = Executor(mode=ExecutionMode.PROPOSE_ONLY, account_number=ACCOUNT)
        with pytest.raises(ValueError, match="sell_to_open"):
            executor._check_order_type("sell_to_open")

    def test_check_order_type_allows_buy_to_open(self):
        executor = Executor(mode=ExecutionMode.PROPOSE_ONLY, account_number=ACCOUNT)
        executor._check_order_type("buy_to_open")  # no exception

    def test_check_order_type_allows_sell_to_close(self):
        executor = Executor(mode=ExecutionMode.PROPOSE_ONLY, account_number=ACCOUNT)
        executor._check_order_type("sell_to_close")  # no exception


# ---------------------------------------------------------------------------
# _resolve_option_id
# ---------------------------------------------------------------------------


class TestResolveOptionId:
    async def test_calls_get_option_instruments_with_correct_params(self):
        rh = _rh_tools()
        executor = Executor(
            mode=ExecutionMode.AUTONOMOUS,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        contract = _make_contract()
        option_id = await executor._resolve_option_id(contract)

        assert option_id == FAKE_OPTION_ID
        rh["get_option_instruments"].ainvoke.assert_called_once()
        call_kwargs = rh["get_option_instruments"].ainvoke.call_args[0][0]
        assert call_kwargs["chain_symbol"] == "AAPL"
        assert call_kwargs["expiration_dates"] == "2026-07-25"
        assert call_kwargs["type"] == "call"
        assert call_kwargs["state"] == "active"

    async def test_raises_when_no_instruments_found(self):
        rh = _rh_tools(instruments_response={"results": []})
        executor = Executor(
            mode=ExecutionMode.AUTONOMOUS,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        with pytest.raises(ValueError, match="No active option instrument"):
            await executor._resolve_option_id(_make_contract())

    async def test_handles_data_key_in_response(self):
        rh = _rh_tools(instruments_response={"data": [{"id": "from-data-key"}]})
        executor = Executor(
            mode=ExecutionMode.AUTONOMOUS,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        result = await executor._resolve_option_id(_make_contract())
        assert result == "from-data-key"

    async def test_handles_list_response(self):
        rh = _rh_tools(instruments_response=[{"id": "from-list"}])
        executor = Executor(
            mode=ExecutionMode.AUTONOMOUS,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        result = await executor._resolve_option_id(_make_contract())
        assert result == "from-list"

    async def test_unwraps_mcp_content_envelope(self):
        # Regression: the raw MCP envelope [{'type','text','id':'lc_...'}] was
        # treated as the instruments list, sending the langchain block id to
        # Robinhood as the option_id.
        import json as _json
        envelope = [{
            "type": "text",
            "text": _json.dumps({"data": {"results": [{"id": FAKE_OPTION_ID}]}}),
            "id": "lc_deadbeef-0000-0000-0000-000000000000",
        }]
        rh = _rh_tools(instruments_response=envelope)
        executor = Executor(
            mode=ExecutionMode.AUTONOMOUS,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        result = await executor._resolve_option_id(_make_contract())
        assert result == FAKE_OPTION_ID

    async def test_handles_nested_data_results(self):
        rh = _rh_tools(instruments_response={"data": {"results": [{"id": "nested"}]}})
        executor = Executor(
            mode=ExecutionMode.AUTONOMOUS,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        result = await executor._resolve_option_id(_make_contract())
        assert result == "nested"

    async def test_raises_when_items_lack_id(self):
        rh = _rh_tools(instruments_response=[{"not_id": "x"}])
        executor = Executor(
            mode=ExecutionMode.AUTONOMOUS,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        with pytest.raises(ValueError, match="No active option instrument"):
            await executor._resolve_option_id(_make_contract())


# ---------------------------------------------------------------------------
# _build_order_params
# ---------------------------------------------------------------------------


class TestBuildOrderParams:
    def _make_request(self) -> OrderRequest:
        candidate = _make_candidate()
        return OrderRequest(
            candidate=candidate,
            action="buy_to_open",
            quantity=2,
            limit_price=Decimal("3.00"),
            mode=ExecutionMode.AUTONOMOUS,
        )

    def test_buy_to_open_maps_to_buy_open(self):
        executor = Executor(mode=ExecutionMode.AUTONOMOUS, account_number=ACCOUNT)
        params = executor._build_order_params(self._make_request(), FAKE_OPTION_ID, for_review=True)
        leg = params["legs"][0]
        assert leg["side"] == "buy"
        assert leg["position_effect"] == "open"

    def test_sell_to_close_maps_to_sell_close(self):
        executor = Executor(mode=ExecutionMode.AUTONOMOUS, account_number=ACCOUNT)
        request = self._make_request()
        request = request.model_copy(update={"action": "sell_to_close"})
        params = executor._build_order_params(request, FAKE_OPTION_ID, for_review=True)
        leg = params["legs"][0]
        assert leg["side"] == "sell"
        assert leg["position_effect"] == "close"

    def test_quantity_and_price_encoded_as_strings(self):
        executor = Executor(mode=ExecutionMode.AUTONOMOUS, account_number=ACCOUNT)
        params = executor._build_order_params(self._make_request(), FAKE_OPTION_ID, for_review=True)
        assert params["quantity"] == "2"
        assert params["price"] == "3.00"

    def test_account_number_included(self):
        executor = Executor(mode=ExecutionMode.AUTONOMOUS, account_number=ACCOUNT)
        params = executor._build_order_params(self._make_request(), FAKE_OPTION_ID, for_review=True)
        assert params["account_number"] == ACCOUNT

    def test_review_params_have_chain_symbol_but_no_ref_id(self):
        # review_option_order schema accepts chain_symbol/underlying_type but
        # rejects ref_id (additionalProperties: false)
        executor = Executor(mode=ExecutionMode.AUTONOMOUS, account_number=ACCOUNT)
        params = executor._build_order_params(self._make_request(), FAKE_OPTION_ID, for_review=True)
        assert params["chain_symbol"] == "AAPL"
        assert params["underlying_type"] == "equity"
        assert "ref_id" not in params

    def test_place_params_have_ref_id_but_no_chain_symbol(self):
        # place_option_order schema accepts ref_id but rejects
        # chain_symbol/underlying_type (additionalProperties: false)
        executor = Executor(mode=ExecutionMode.AUTONOMOUS, account_number=ACCOUNT)
        request = self._make_request()
        params = executor._build_order_params(request, FAKE_OPTION_ID, for_review=False)
        assert params["ref_id"] == request.ref_id
        assert "chain_symbol" not in params
        assert "underlying_type" not in params

    def test_option_id_in_leg(self):
        executor = Executor(mode=ExecutionMode.AUTONOMOUS, account_number=ACCOUNT)
        params = executor._build_order_params(self._make_request(), FAKE_OPTION_ID, for_review=True)
        assert params["legs"][0]["option_id"] == FAKE_OPTION_ID

    def test_order_type_is_limit_gfd(self):
        executor = Executor(mode=ExecutionMode.AUTONOMOUS, account_number=ACCOUNT)
        params = executor._build_order_params(self._make_request(), FAKE_OPTION_ID, for_review=True)
        assert params["type"] == "limit"
        assert params["time_in_force"] == "gfd"


# ---------------------------------------------------------------------------
# autonomous mode
# ---------------------------------------------------------------------------


class TestAutonomous:
    async def test_extracts_order_id_from_live_place_response_shape(self):
        # Exact nesting from the live RH MCP place_option_order response —
        # the first real order was misreported as not-placed because the id
        # lives at data.order.id, not data.id.
        rh = _rh_tools(place_response={"data": {"order": {
            "id": "6a57a2c5-ba56-40c3-9624-d0862beda75d",
            "chain_symbol": "HOOD", "state": "unconfirmed", "type": "limit",
            "legs": [{"option_id": "186ed0b7", "side": "buy", "position_effect": "open"}],
        }}})
        executor = Executor(
            mode=ExecutionMode.AUTONOMOUS,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        result = await executor.execute(_make_candidate())
        assert result.placed is True
        assert result.order_id == "6a57a2c5-ba56-40c3-9624-d0862beda75d"

    async def test_place_response_without_order_id_reports_not_placed(self):
        # An error payload from place_option_order has no order id; claiming
        # placed=True would create a phantom tracked position.
        rh = _rh_tools(place_response={"errors": ["rejected upstream"]})
        executor = Executor(
            mode=ExecutionMode.AUTONOMOUS,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        result = await executor.execute(_make_candidate())
        assert result.placed is False
        assert result.order_id is None
        assert "no_order_id_in_response" in (result.rejection_reason or "")

    async def test_places_order_when_no_blocking_alerts(self):
        rh = _rh_tools()
        executor = Executor(
            mode=ExecutionMode.AUTONOMOUS,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        result = await executor.execute(_make_candidate())

        assert result.placed is True
        assert result.order_id == FAKE_ORDER_ID
        rh["review_option_order"].ainvoke.assert_called_once()
        rh["place_option_order"].ainvoke.assert_called_once()

    async def test_calls_get_option_instruments_first(self):
        rh = _rh_tools()
        executor = Executor(
            mode=ExecutionMode.AUTONOMOUS,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        await executor.execute(_make_candidate())
        rh["get_option_instruments"].ainvoke.assert_called_once()

    async def test_blocked_by_fatal_alert_does_not_place(self):
        review = {"order_checks": [{"severity": "fatal", "detail": "Account restricted"}]}
        rh = _rh_tools(review_response=review)
        executor = Executor(
            mode=ExecutionMode.AUTONOMOUS,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        result = await executor.execute(_make_candidate())

        assert result.placed is False
        assert "blocked_by_alerts" in result.rejection_reason
        assert "Account restricted" in result.rejection_reason
        rh["place_option_order"].ainvoke.assert_not_called()

    async def test_warning_alerts_do_not_block(self):
        review = {"order_checks": [{"severity": "warning", "detail": "Low liquidity"}]}
        rh = _rh_tools(review_response=review)
        executor = Executor(
            mode=ExecutionMode.AUTONOMOUS,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        result = await executor.execute(_make_candidate())
        assert result.placed is True

    async def test_review_summary_populated(self):
        review = {"order_checks": [], "quote": {"mid_price": "3.00"}}
        rh = _rh_tools(review_response=review)
        executor = Executor(
            mode=ExecutionMode.AUTONOMOUS,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        result = await executor.execute(_make_candidate())
        assert result.review_summary is not None

    async def test_review_and_place_params_match_their_schemas(self):
        rh = _rh_tools()
        executor = Executor(
            mode=ExecutionMode.AUTONOMOUS,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        await executor.execute(_make_candidate())

        review_params = rh["review_option_order"].ainvoke.call_args[0][0]
        place_params = rh["place_option_order"].ainvoke.call_args[0][0]
        # shared order fields identical
        for key in ("account_number", "quantity", "legs", "type", "time_in_force", "price"):
            assert review_params[key] == place_params[key]
        # endpoint-specific fields: strict MCP schemas reject the others'
        assert "ref_id" not in review_params and "chain_symbol" in review_params
        assert "ref_id" in place_params and "chain_symbol" not in place_params

    async def test_result_timestamp_is_utc(self):
        rh = _rh_tools()
        executor = Executor(
            mode=ExecutionMode.AUTONOMOUS,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        result = await executor.execute(_make_candidate())
        assert result.timestamp.tzinfo is not None


# ---------------------------------------------------------------------------
# rh_approval mode
# ---------------------------------------------------------------------------


class TestRhApproval:
    async def test_approved_places_order(self):
        rh = _rh_tools()
        executor = Executor(
            mode=ExecutionMode.RH_APPROVAL,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        with patch("trader.executor.executor.interrupt", return_value="approve"):
            result = await executor.execute(_make_candidate())

        assert result.placed is True
        assert result.order_id == FAKE_ORDER_ID
        rh["place_option_order"].ainvoke.assert_called_once()

    async def test_rejected_does_not_place(self):
        rh = _rh_tools()
        executor = Executor(
            mode=ExecutionMode.RH_APPROVAL,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        with patch("trader.executor.executor.interrupt", return_value="no thanks"):
            result = await executor.execute(_make_candidate())

        assert result.placed is False
        assert "user_rejected" in result.rejection_reason
        rh["place_option_order"].ainvoke.assert_not_called()

    async def test_review_called_before_interrupt(self):
        rh = _rh_tools()
        executor = Executor(
            mode=ExecutionMode.RH_APPROVAL,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        with patch("trader.executor.executor.interrupt", return_value="approve"):
            await executor.execute(_make_candidate())

        rh["review_option_order"].ainvoke.assert_called_once()

    async def test_interrupt_payload_contains_ticker_and_review(self):
        rh = _rh_tools()
        executor = Executor(
            mode=ExecutionMode.RH_APPROVAL,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        with patch("trader.executor.executor.interrupt", return_value="approve") as mock_interrupt:
            await executor.execute(_make_candidate())

        payload = mock_interrupt.call_args[0][0]
        assert payload["type"] == "rh_order_review"
        assert payload["ticker"] == "AAPL"
        assert "review" in payload
        assert "review_summary" in payload
        assert "prompt" in payload

    async def test_approve_case_insensitive(self):
        rh = _rh_tools()
        executor = Executor(
            mode=ExecutionMode.RH_APPROVAL,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        for response in ("APPROVE", "Approve", " approve "):
            rh["place_option_order"].ainvoke.reset_mock()
            with patch("trader.executor.executor.interrupt", return_value=response):
                result = await executor.execute(_make_candidate())
            assert result.placed is True, f"Expected placed=True for response={response!r}"

    async def test_rejection_reason_includes_user_response(self):
        rh = _rh_tools()
        executor = Executor(
            mode=ExecutionMode.RH_APPROVAL,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        with patch("trader.executor.executor.interrupt", return_value="reject — too risky"):
            result = await executor.execute(_make_candidate())

        assert "reject — too risky" in result.rejection_reason

    async def test_review_summary_included_in_both_outcomes(self):
        review = {"order_checks": [], "quote": {"mid_price": "3.00"}}
        rh = _rh_tools(review_response=review)
        executor = Executor(
            mode=ExecutionMode.RH_APPROVAL,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        for decision in ("approve", "reject"):
            with patch("trader.executor.executor.interrupt", return_value=decision):
                result = await executor.execute(_make_candidate())
            assert result.review_summary is not None


# ---------------------------------------------------------------------------
# execute_orders graph node (integration-level unit test)
# ---------------------------------------------------------------------------


class TestExecuteOrdersNode:
    async def test_skips_non_proposed_candidates(self):
        from trader.graph.agent import execute_orders
        from trader.graph.state import TradingAgentState

        executor = Executor(mode=ExecutionMode.PROPOSE_ONLY, account_number=ACCOUNT)
        candidate = _make_candidate(status="skipped_risk_gate")

        state = TradingAgentState(candidates=[candidate])
        updates = await execute_orders(state, executor)

        assert updates["candidates"][0].execution_status == "skipped_risk_gate"
        assert updates["order_results"] == []

    async def test_propose_only_leaves_status_proposed(self):
        from trader.graph.agent import execute_orders
        from trader.graph.state import TradingAgentState

        executor = Executor(mode=ExecutionMode.PROPOSE_ONLY, account_number=ACCOUNT)
        state = TradingAgentState(candidates=[_make_candidate()])
        updates = await execute_orders(state, executor)

        assert updates["candidates"][0].execution_status == "proposed"
        assert len(updates["order_results"]) == 1
        assert updates["order_results"][0].placed is False

    async def test_autonomous_executed_sets_status(self):
        from trader.graph.agent import execute_orders
        from trader.graph.state import TradingAgentState

        rh = _rh_tools()
        executor = Executor(
            mode=ExecutionMode.AUTONOMOUS,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        state = TradingAgentState(candidates=[_make_candidate()])
        updates = await execute_orders(state, executor)

        assert updates["candidates"][0].execution_status == "executed"
        assert updates["order_results"][0].placed is True

    async def test_execute_exception_preserves_candidate(self):
        from trader.graph.agent import execute_orders
        from trader.graph.state import TradingAgentState

        rh = _rh_tools(instruments_response={"results": []})  # will raise ValueError
        executor = Executor(
            mode=ExecutionMode.AUTONOMOUS,
            account_number=ACCOUNT,
            rh_tools=rh,
        )
        state = TradingAgentState(candidates=[_make_candidate()])
        updates = await execute_orders(state, executor)

        assert updates["candidates"][0].execution_status == "proposed"
        assert updates["order_results"][0].placed is False
        assert "No active option instrument" in updates["order_results"][0].rejection_reason

    async def test_mixed_candidate_batch(self):
        from trader.graph.agent import execute_orders
        from trader.graph.state import TradingAgentState

        executor = Executor(mode=ExecutionMode.PROPOSE_ONLY, account_number=ACCOUNT)
        c_proposed = _make_candidate(status="proposed")
        c_skipped = _make_candidate(status="skipped_risk_gate")
        c_no_contract = _make_candidate(status="not_executable_long_only", with_contract=False)

        state = TradingAgentState(candidates=[c_proposed, c_skipped, c_no_contract])
        updates = await execute_orders(state, executor)

        statuses = [c.execution_status for c in updates["candidates"]]
        assert statuses[0] == "proposed"     # executed as propose_only → stays proposed
        assert statuses[1] == "skipped_risk_gate"  # passed through
        assert statuses[2] == "not_executable_long_only"  # no contract → passed through
        assert len(updates["order_results"]) == 1  # only the proposed one ran
