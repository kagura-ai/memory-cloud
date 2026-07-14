"""Unit tests for agent correlation parsing + identity precedence (Issue #1277).

Pins the W3C traceparent/baggage parsers, token validation + server-side
generation, and — the load-bearing part — the normative identity-precedence
rules (F4): a claim never outranks a credential; baggage claims verify only
via same-member binding and stamp policy_decision='unbound'; unverified
claims reach only a keyed-hash metadata field, never the agent_id column.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.correlation import (
    POLICY_DECISION_UNBOUND,
    CorrelationContext,
    build_correlation_from_headers,
    parse_baggage,
    parse_traceparent,
    resolve_agent_correlation,
    validate_correlation_token,
)

WORKSPACE_ID = uuid.uuid4()
CRED_AGENT = uuid.uuid4()
OTHER_AGENT = uuid.uuid4()
VALID_TP = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


class TestTraceparent:
    def test_valid(self):
        assert parse_traceparent(VALID_TP) == (
            "4bf92f3577b34da6a3ce929d0e0e4736",
            "00f067aa0ba902b7",
        )

    @pytest.mark.parametrize(
        "bad",
        [
            None,
            "",
            "garbage",
            "00-4bf9-00f067aa0ba902b7-01",  # short trace
            "00-" + "0" * 32 + "-00f067aa0ba902b7-01",  # all-zero trace
            "00-4bf92f3577b34da6a3ce929d0e0e4736-" + "0" * 16 + "-01",  # all-zero span
            "00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01",  # uppercase
        ],
    )
    def test_invalid_returns_none(self, bad):
        assert parse_traceparent(bad) is None


class TestBaggage:
    def test_parses_keys_and_strips_properties(self):
        bag = parse_baggage(
            "gen_ai.agent.id=a1,gen_ai.conversation.id=s1;meta=x, kagura.agent.run.id=r1"
        )
        assert bag["gen_ai.agent.id"] == "a1"
        assert bag["gen_ai.conversation.id"] == "s1"
        assert bag["kagura.agent.run.id"] == "r1"

    def test_malformed_entries_skipped(self):
        assert parse_baggage("no-equals,=noval,k=v") == {"k": "v"}

    def test_none(self):
        assert parse_baggage(None) == {}


class TestTokenValidation:
    @pytest.mark.parametrize("ok", ["sess-1", "a.b_c-D9", "x" * 128])
    def test_valid(self, ok):
        assert validate_correlation_token(ok) == ok

    @pytest.mark.parametrize("bad", [None, "", "x" * 129, "has space", "sess/1", "sess:1", 42])
    def test_invalid_dropped(self, bad):
        assert validate_correlation_token(bad) is None


class TestBuild:
    def test_generates_ids_when_no_traceparent(self):
        c = build_correlation_from_headers(traceparent=None, baggage=None)
        assert len(c.trace_id) == 32 and len(c.span_id) == 16
        assert c.session_id is None and c.run_id is None

    def test_session_alias_accepted(self):
        c = build_correlation_from_headers(traceparent=None, baggage="session.id=s2")
        assert c.session_id == "s2"

    def test_invalid_session_token_dropped(self):
        c = build_correlation_from_headers(
            traceparent=None, baggage="gen_ai.conversation.id=has space"
        )
        assert c.session_id is None


# ---------------------------------------------------------------------------
# Identity precedence (the normative core)
# ---------------------------------------------------------------------------


class TestPrecedence:
    @pytest.mark.asyncio
    async def test_credential_bound_always_wins_over_disagreeing_baggage(self):
        corr = CorrelationContext(trace_id="t", span_id="s", agent_claim=str(OTHER_AGENT))
        res = await resolve_agent_correlation(
            MagicMock(),
            credential_agent_id=CRED_AGENT,
            explicit_agent_id=None,
            member_user_id="m",
            workspace_id=WORKSPACE_ID,
            correlation=corr,
        )
        # Credential wins; the disagreeing claim is only a keyed hash.
        assert res.agent_id == CRED_AGENT
        assert res.unverified_agent_claim_hash is not None
        assert res.policy_decision is None

    @pytest.mark.asyncio
    async def test_credential_bound_disagreeing_explicit_arg_hashed_not_used(self):
        res = await resolve_agent_correlation(
            MagicMock(),
            credential_agent_id=CRED_AGENT,
            explicit_agent_id=OTHER_AGENT,
            member_user_id="m",
            workspace_id=WORKSPACE_ID,
        )
        assert res.agent_id == CRED_AGENT
        assert res.unverified_agent_claim_hash is not None

    @pytest.mark.asyncio
    async def test_unbound_credential_verified_claim_stamps_unbound(self, monkeypatch):
        # Same-member verification passes → claim populates agent_id + 'unbound'.
        monkeypatch.setattr(
            "api.correlation.verify_baggage_agent_claim",
            AsyncMock(return_value=True),
        )
        corr = CorrelationContext(trace_id="t", span_id="s", agent_claim=str(OTHER_AGENT))
        res = await resolve_agent_correlation(
            MagicMock(),
            credential_agent_id=None,
            explicit_agent_id=None,
            member_user_id="m",
            workspace_id=WORKSPACE_ID,
            correlation=corr,
        )
        assert res.agent_id == OTHER_AGENT
        assert res.policy_decision == POLICY_DECISION_UNBOUND

    @pytest.mark.asyncio
    async def test_unbound_credential_unverified_claim_hashed_only(self, monkeypatch):
        monkeypatch.setattr(
            "api.correlation.verify_baggage_agent_claim",
            AsyncMock(return_value=False),
        )
        corr = CorrelationContext(trace_id="t", span_id="s", agent_claim=str(OTHER_AGENT))
        res = await resolve_agent_correlation(
            MagicMock(),
            credential_agent_id=None,
            explicit_agent_id=None,
            member_user_id="m",
            workspace_id=WORKSPACE_ID,
            correlation=corr,
        )
        assert res.agent_id is None  # never reaches the column
        assert res.unverified_agent_claim_hash is not None
        assert res.policy_decision is None

    @pytest.mark.asyncio
    async def test_unbound_credential_explicit_vs_baggage_sets_conflict(self, monkeypatch):
        monkeypatch.setattr(
            "api.correlation.verify_baggage_agent_claim",
            AsyncMock(return_value=True),
        )
        corr = CorrelationContext(trace_id="t", span_id="s", agent_claim=str(uuid.uuid4()))
        res = await resolve_agent_correlation(
            MagicMock(),
            credential_agent_id=None,
            explicit_agent_id=OTHER_AGENT,  # disagrees with baggage claim
            member_user_id="m",
            workspace_id=WORKSPACE_ID,
            correlation=corr,
        )
        # Rule 5: explicit wins; conflict flagged.
        assert res.agent_id == OTHER_AGENT
        assert res.correlation_conflict is True


class TestVerifyPredicate:
    @pytest.mark.asyncio
    async def test_verifies_only_same_member_bound_agent(self):
        from api.correlation import verify_baggage_agent_claim

        db = MagicMock()
        # agent exists in workspace, and a same-member non-revoked key is bound.
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=OTHER_AGENT)),
                MagicMock(first=MagicMock(return_value=(1,))),
            ]
        )
        assert await verify_baggage_agent_claim(
            db, claimed_agent_id=OTHER_AGENT, member_user_id="m", workspace_id=WORKSPACE_ID
        )

    @pytest.mark.asyncio
    async def test_rejects_when_no_same_member_key(self):
        from api.correlation import verify_baggage_agent_claim

        db = MagicMock()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=OTHER_AGENT)),
                MagicMock(first=MagicMock(return_value=None)),
            ]
        )
        assert not await verify_baggage_agent_claim(
            db, claimed_agent_id=OTHER_AGENT, member_user_id="m", workspace_id=WORKSPACE_ID
        )

    @pytest.mark.asyncio
    async def test_rejects_when_agent_not_in_workspace(self):
        from api.correlation import verify_baggage_agent_claim

        db = MagicMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        )
        assert not await verify_baggage_agent_claim(
            db, claimed_agent_id=OTHER_AGENT, member_user_id="m", workspace_id=WORKSPACE_ID
        )
