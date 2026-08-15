"""Integration tests for the /api/backtest dashboard endpoint."""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from trader.executor.executor import Executor
from trader.executor.schemas import ExecutionMode
from trader.live.approval_server import create_app
from trader.live.proposals import ProposalStore


def _make_app(results_file):
    return create_app(
        proposal_store=ProposalStore(),
        executor=Executor(mode=ExecutionMode.PROPOSE_ONLY, account_number="TEST"),
        backtest_results_file=results_file,
    )


@pytest.fixture
async def client(tmp_path):
    results_file = tmp_path / "backtest_results.json"
    app = _make_app(results_file)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    client.results_file = results_file
    yield client
    await client.close()


class TestGetBacktestEndpoint:
    async def test_no_results_file_returns_not_yet_run(self, client):
        resp = await client.get("/api/backtest")
        assert resp.status == 200
        data = await resp.json()
        assert data == {"status": "not_yet_run"}

    async def test_returns_persisted_results_verbatim(self, client):
        payload = {
            "run_at": "2026-08-15T21:00:00+00:00",
            "tickers": ["AAPL", "SPY"],
            "trading_days": 18,
            "all_time": {"overall": {"trade_count": 5, "win_rate": 0.6}},
            "trades": [],
        }
        client.results_file.write_text(json.dumps(payload))

        resp = await client.get("/api/backtest")
        assert resp.status == 200
        data = await resp.json()
        assert data == payload

    async def test_corrupt_results_file_degrades_to_not_yet_run(self, client):
        client.results_file.write_text("{not valid json")

        resp = await client.get("/api/backtest")
        assert resp.status == 200
        data = await resp.json()
        assert data == {"status": "not_yet_run"}


class TestDashboardHtmlIncludesBacktestTab:
    async def test_dashboard_page_renders_backtest_tab(self, client):
        resp = await client.get("/")
        assert resp.status == 200
        html = await resp.text()
        assert 'data-tab="backtest"' in html
        assert 'id="tab-backtest"' in html
        assert "loadBacktest" in html
