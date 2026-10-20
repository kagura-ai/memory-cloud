"""Where a write's agent and session come from (#2044).

- ``agent_id`` only from the verified credential (``AgentScope``); a baggage
  agent claim never reaches it, on REST or MCP.
- ``session_id`` from the request's correlation (OTel baggage); on the
  stateful MCP transport the protocol session fills in when baggage named
  none, and baggage wins when it did.
- Every revision row and the memory's last-writer columns carry both.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.correlation import CorrelationContext, get_correlation, set_correlation
from auth.agent_scope import AgentScope, set_agent_scope
from models.memory import MemoryRevision
from services.memory_versions import record_revision, revision_values, stamp_writer
from services.write_provenance import (
    SessionPrincipal,
    WriteProvenance,
    caller_principal,
    current_provenance,
)

AGENT = uuid.uuid4()


@pytest.fixture(autouse=True)
def _clean() -> None:
    set_agent_scope(None)
    set_correlation(None)
    yield
    set_agent_scope(None)
    set_correlation(None)


def _corr(session_id: str | None = None, agent_claim: str | None = None) -> CorrelationContext:
    return CorrelationContext(
        trace_id="0" * 31 + "1",
        span_id="0" * 15 + "1",
        session_id=session_id,
        agent_claim=agent_claim,
    )


def test_outside_a_request_there_is_no_provenance():
    assert current_provenance() == WriteProvenance(None, None)
    assert caller_principal("u1") == SessionPrincipal("u1", None)


def test_agent_comes_from_the_credential_and_session_from_baggage():
    set_agent_scope(AgentScope(agent_id=AGENT, enforcement_mode="enforce"))
    set_correlation(_corr(session_id="run-42"))
    assert current_provenance() == WriteProvenance(AGENT, "run-42")
    assert caller_principal("u1") == SessionPrincipal("u1", AGENT)


def test_a_baggage_agent_claim_never_becomes_the_agent():
    set_correlation(_corr(session_id="s", agent_claim=str(AGENT)))
    assert current_provenance() == WriteProvenance(None, "s")
    assert caller_principal("u1").agent_id is None


def test_rest_baggage_reaches_the_session_but_not_the_agent():
    """The REST seam: CorrelationMiddleware parses baggage; a claimed agent id
    in it stays out of the provenance (no credential-bound AgentScope)."""
    from api.middleware.correlation import CorrelationMiddleware

    app = FastAPI()
    app.add_middleware(CorrelationMiddleware)

    @app.get("/api/v1/probe")
    async def probe() -> dict[str, str | None]:
        prov = current_provenance()
        return {
            "agent": str(prov.agent_id) if prov.agent_id else None,
            "session": prov.session_id,
        }

    client = TestClient(app)
    body = client.get(
        "/api/v1/probe",
        headers={"baggage": f"session.id=rest-session,gen_ai.agent.id={AGENT}"},
    ).json()
    assert body == {"agent": None, "session": "rest-session"}
    malformed = client.get("/api/v1/probe", headers={"baggage": "session.id=has%20space"}).json()
    assert malformed == {"agent": None, "session": None}


def test_mcp_protocol_session_fills_in_only_without_baggage():
    from mcp_server.transport import _default_correlation_session

    set_correlation(_corr())
    _default_correlation_session("mcp-0123456789abcdef")
    assert get_correlation().session_id == "mcp-0123456789abcdef"

    set_correlation(_corr(session_id="from-baggage"))
    _default_correlation_session("mcp-0123456789abcdef")
    assert get_correlation().session_id == "from-baggage"

    # An id that fails the token check is dropped, never stored.
    set_correlation(_corr())
    _default_correlation_session("bad id!")
    assert get_correlation().session_id is None

    set_correlation(None)
    _default_correlation_session("mcp-0123456789abcdef")  # no correlation: no-op
    assert get_correlation() is None


def test_record_revision_stamps_the_row_and_the_memory():
    set_agent_scope(AgentScope(agent_id=AGENT, enforcement_mode="enforce"))
    set_correlation(_corr(session_id="sess-1"))
    db = MagicMock()
    memory = SimpleNamespace(
        id=uuid.uuid4(), context_id=uuid.uuid4(), version=2, agent_id=None, session_id="old"
    )
    record_revision(db, memory, kind="updated", actor_user_id="u1", prior={"summary": "a"})
    (row,) = [c.args[0] for c in db.add.call_args_list]
    assert isinstance(row, MemoryRevision)
    assert (row.agent_id, row.session_id, row.actor_user_id) == (AGENT, "sess-1", "u1")
    assert (memory.agent_id, memory.session_id) == (AGENT, "sess-1")


def test_explicit_provenance_wins():
    db = MagicMock()
    memory = SimpleNamespace(
        id=uuid.uuid4(), context_id=uuid.uuid4(), version=1, agent_id=AGENT, session_id=None
    )
    record_revision(
        db,
        memory,
        kind="created",
        actor_user_id="u1",
        provenance=WriteProvenance(None, "explicit"),
    )
    (row,) = [c.args[0] for c in db.add.call_args_list]
    assert (row.agent_id, row.session_id) == (None, "explicit")
    assert memory.session_id == "explicit" and memory.agent_id is None


def test_a_contextless_row_is_neither_logged_nor_stamped():
    """v0.113.0 review F4: no revision row, so no session token either —
    erasure scrubs ``memories.session_id`` through the subject's revision
    rows, and a token stamped without one would survive it."""
    db = MagicMock()
    memory = SimpleNamespace(
        id=uuid.uuid4(), context_id=None, version=1, agent_id=AGENT, session_id="stale"
    )
    record_revision(
        db,
        memory,
        kind="updated",
        actor_user_id="u1",
        provenance=WriteProvenance(AGENT, "sess-x"),
    )
    db.add.assert_not_called()  # no context, no revision (PR1 rule)
    assert memory.session_id is None and memory.agent_id is None  # a stale stamp is cleared
    stamp_writer(memory, WriteProvenance(AGENT, "sess-x"))
    assert memory.session_id is None and memory.agent_id is None


def test_revision_values_carry_the_provenance_and_writer():
    values = revision_values(
        memory_id=uuid.uuid4(),
        context_id=uuid.uuid4(),
        version=1,
        kind="forgotten",
        actor_user_id="u1",
        writer="rollback",
        provenance=WriteProvenance(AGENT, "s"),
    )
    assert (values["agent_id"], values["session_id"], values["writer"]) == (AGENT, "s", "rollback")


# ----------------------------------------------------- #2114 linked identities


def test_a_principal_defaults_to_its_own_sub():
    assert SessionPrincipal("u1", None).actor_user_ids == frozenset({"u1"})
    assert SessionPrincipal("u1", None, frozenset({"u1"})) == SessionPrincipal("u1", None)
    with pytest.raises(ValueError, match="include"):
        SessionPrincipal("u1", None, frozenset({"u2"}))


def test_principal_predicate_is_equality_for_one_sub_and_in_for_a_set():
    from sqlalchemy.dialects import postgresql

    from services.write_provenance import principal_predicate

    def sql(principal: SessionPrincipal) -> str:
        clause = principal_predicate(
            MemoryRevision.actor_user_id, MemoryRevision.agent_id, principal
        )
        return str(
            clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )

    one = sql(SessionPrincipal("u1", None))
    assert "memory_revisions.actor_user_id = 'u1'" in one and " IN " not in one
    several = sql(SessionPrincipal("u2", AGENT, frozenset({"u2", "u1"})))
    assert "memory_revisions.actor_user_id IN ('u1', 'u2')" in several
    assert f"memory_revisions.agent_id = '{AGENT}'" in several


@pytest.mark.asyncio
async def test_caller_principal_linked_resolves_the_link_set():
    from unittest.mock import AsyncMock, patch

    from services.write_provenance import caller_principal_linked

    db = MagicMock()
    alone = AsyncMock(return_value=frozenset({"u1"}))
    with patch("services.identity_link_service.linked_user_ids", alone):
        assert await caller_principal_linked(db, "u1") == SessionPrincipal("u1", None)
    alone.assert_awaited_once_with(db, "u1")

    linked = AsyncMock(return_value=frozenset({"u1", "u2"}))
    set_agent_scope(AgentScope(agent_id=AGENT, enforcement_mode="enforce"))
    with patch("services.identity_link_service.linked_user_ids", linked):
        principal = await caller_principal_linked(db, "u1")
    # The own sub stays the stamped one; the agent never widens.
    assert principal == SessionPrincipal("u1", AGENT, frozenset({"u1", "u2"}))
    assert principal.canonical_user_id == "u1"
    # Write provenance stays the raw sub.
    assert caller_principal("u1").actor_user_ids == frozenset({"u1"})
