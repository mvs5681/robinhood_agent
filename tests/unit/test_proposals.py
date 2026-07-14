"""Tests for ProposalStore transition semantics and duplicate-signal gating."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from trader.live.proposals import ProposalStore


def _candidate(ticker: str = "NVDA") -> MagicMock:
    c = MagicMock()
    c.ticker = ticker
    return c


class TestApproveSemantics:
    async def test_approve_pending_returns_proposal(self):
        store = ProposalStore()
        p = await store.add(_candidate())
        approved = await store.approve(p.proposal_id)
        assert approved is not None
        assert approved.status == "approved"

    async def test_second_approve_returns_none(self):
        store = ProposalStore()
        p = await store.add(_candidate())
        assert await store.approve(p.proposal_id) is not None
        assert await store.approve(p.proposal_id) is None

    async def test_approve_after_reject_returns_none(self):
        store = ProposalStore()
        p = await store.add(_candidate())
        assert await store.reject(p.proposal_id) is not None
        assert await store.approve(p.proposal_id) is None

    async def test_approve_expired_returns_none_and_marks_expired(self):
        store = ProposalStore()
        p = await store.add(_candidate())
        p.created_at = datetime.now(timezone.utc) - timedelta(seconds=store.TTL_SECONDS + 1)
        assert await store.approve(p.proposal_id) is None
        assert p.status == "expired"

    async def test_approve_unknown_id_returns_none(self):
        store = ProposalStore()
        assert await store.approve("nope") is None


class TestHasRecent:
    async def test_true_for_fresh_proposal(self):
        store = ProposalStore()
        await store.add(_candidate("NVDA"))
        assert await store.has_recent("NVDA") is True

    async def test_false_for_other_ticker(self):
        store = ProposalStore()
        await store.add(_candidate("NVDA"))
        assert await store.has_recent("SPY") is False

    async def test_true_even_after_decision(self):
        store = ProposalStore()
        p = await store.add(_candidate("NVDA"))
        await store.reject(p.proposal_id)
        assert await store.has_recent("NVDA") is True

    async def test_false_once_outside_ttl_window(self):
        store = ProposalStore()
        p = await store.add(_candidate("NVDA"))
        p.created_at = datetime.now(timezone.utc) - timedelta(seconds=store.TTL_SECONDS + 1)
        assert await store.has_recent("NVDA") is False


class TestPrune:
    async def test_decided_proposals_pruned_after_retention(self):
        store = ProposalStore()
        p = await store.add(_candidate("NVDA"))
        await store.reject(p.proposal_id)
        p.decided_at = datetime.now(timezone.utc) - timedelta(seconds=store.RETENTION_SECONDS + 1)
        await store.add(_candidate("SPY"))  # add() prunes
        assert await store.get(p.proposal_id) is None

    async def test_pending_never_pruned(self):
        store = ProposalStore()
        p = await store.add(_candidate("NVDA"))
        p.created_at = datetime.now(timezone.utc) - timedelta(seconds=store.RETENTION_SECONDS + 1)
        await store.add(_candidate("SPY"))
        assert await store.get(p.proposal_id) is not None
