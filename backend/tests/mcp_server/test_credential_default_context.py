"""A default context bound to the credential (#2129).

A user API key (``api_keys.default_context_id``) and an OAuth grant
(``oauth_grant_settings``) can carry a default context. The MCP auth step
stores it per request; the dispatcher fills a missing ``context_id`` from it
below ``?context=`` and above the agent binding; the server instructions name
it like the URL default. Pinned here:

* precedence — url > credential > binding > none; an invalid URL value is
  never read as "no URL default";
* the auth adapters — an API key and an OAuth grant set it, a connector key
  and a session cookie do not, and every request starts from ``None``;
* the dispatcher — the call runs in the credential default, and a context the
  caller cannot use answers ``context_not_found`` with
  ``context_source: "credential"`` and its own help;
* the instructions — named through the read resolver, private, and the
  digest follows it ahead of the last-used fallback.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

import mcp_server.auth as mcp_auth
import mcp_server.tools as tools_mod
import mcp_server.tools._capacity_gate as capacity_gate
from auth.agent_scope import AgentScope, set_agent_scope
from auth.api_keys import VerifiedKey
from mcp_server.tools import _DEFAULT_CONTEXT_DENIED_HELP, execute_tool_call, get_tool_definitions
from mcp_server.tools._default_context import (
    DefaultContext,
    DefaultContextSelection,
    resolve_default_context,
)
from mcp_server.tools._helpers import (
    get_mcp_credential_default_context,
    set_mcp_credential_default_context,
    set_mcp_default_context_selection,
    set_mcp_key_workspace_scope,
)

CREDENTIAL = uuid4()


@pytest.fixture(autouse=True)
def _clean_contextvars():
    set_mcp_default_context_selection(None)
    set_mcp_credential_default_context(None)
    set_mcp_key_workspace_scope(None)
    set_agent_scope(None)
    yield
    set_mcp_default_context_selection(None)
    set_mcp_credential_default_context(None)
    set_mcp_key_workspace_scope(None)
    set_agent_scope(None)


# ------------------------------------------------------------------ precedence


@pytest.mark.asyncio
async def test_the_url_default_wins_over_the_credential_default():
    url = uuid4()
    set_mcp_credential_default_context(CREDENTIAL)

    default = await resolve_default_context(DefaultContextSelection("url", url))

    assert default == DefaultContext(url, "url")


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [None, DefaultContextSelection("none")])
async def test_without_a_url_default_the_credential_default_applies(selection):
    set_mcp_credential_default_context(CREDENTIAL)

    assert await resolve_default_context(selection) == DefaultContext(CREDENTIAL, "credential")


@pytest.mark.asyncio
async def test_the_credential_default_wins_over_an_agent_binding(monkeypatch):
    """Defensive: the CHECK keeps a default off agent-bound keys; should both
    ever be present the credential's is used and the binding is not read."""
    import mcp_server.tools._default_context as dc

    binding = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(dc, "_agent_default_binding", binding)
    set_agent_scope(AgentScope(agent_id=uuid4(), enforcement_mode="enforce", workspace_id=None))
    set_mcp_credential_default_context(CREDENTIAL)

    assert await resolve_default_context(None) == DefaultContext(CREDENTIAL, "credential")
    binding.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_agent_binding_still_applies_without_a_credential_default(monkeypatch):
    import mcp_server.tools._default_context as dc

    bound = uuid4()
    monkeypatch.setattr(dc, "_agent_default_binding", AsyncMock(return_value=bound))
    set_agent_scope(AgentScope(agent_id=uuid4(), enforcement_mode="enforce", workspace_id=None))

    assert await resolve_default_context(None) == DefaultContext(bound, "binding")


@pytest.mark.asyncio
async def test_no_default_anywhere_is_none():
    assert await resolve_default_context(None) is None


@pytest.mark.asyncio
async def test_an_invalid_url_value_does_not_fall_through_to_the_credential_default():
    set_mcp_credential_default_context(CREDENTIAL)

    assert await resolve_default_context(DefaultContextSelection("invalid", raw_length=3)) is None


# ------------------------------------------------------------------ auth adapters


def _verified(**extra) -> VerifiedKey:
    return VerifiedKey(id=1, user_id="user-1", workspace_id=None, bound_context_id=None, **extra)


@pytest.mark.asyncio
async def test_an_api_key_carries_its_default_context():
    key = _verified(default_context_id=CREDENTIAL)
    with patch("auth.dependencies.verify_api_key", new=AsyncMock(return_value=key)):
        await mcp_auth.authenticate_mcp_request(b"Bearer kagura_x")

    assert get_mcp_credential_default_context() == CREDENTIAL


@pytest.mark.asyncio
async def test_a_connector_key_never_carries_a_default_context():
    key = _verified(default_context_id=CREDENTIAL, is_connector_key=True)
    with patch("auth.dependencies.verify_api_key", new=AsyncMock(return_value=key)):
        await mcp_auth.authenticate_mcp_request(b"Bearer kagura_x")

    assert get_mcp_credential_default_context() is None


@pytest.mark.asyncio
async def test_a_key_without_a_default_resets_a_stale_value():
    set_mcp_credential_default_context(uuid4())
    with patch("auth.dependencies.verify_api_key", new=AsyncMock(return_value=_verified())):
        await mcp_auth.authenticate_mcp_request(b"Bearer kagura_x")

    assert get_mcp_credential_default_context() is None


@pytest.mark.asyncio
async def test_a_session_cookie_never_carries_a_default_context(monkeypatch):
    set_mcp_credential_default_context(uuid4())
    monkeypatch.setattr(mcp_auth, "_verify_session_cookie", AsyncMock(return_value="user-1"))

    await mcp_auth.authenticate_mcp_request(None, cookie_header=b"kagura_session=s")

    assert get_mcp_credential_default_context() is None


@pytest.mark.asyncio
async def test_an_oauth_grant_carries_its_default_context(monkeypatch):
    grant = mcp_auth.OAuthGrant(
        user_id="user-1", scope="memory:read", resource=None, default_context_id=CREDENTIAL
    )
    monkeypatch.setattr(mcp_auth, "_verify_api_key", AsyncMock(return_value=None))
    monkeypatch.setattr(mcp_auth, "_verify_oauth2_token", AsyncMock(return_value=grant))

    await mcp_auth.authenticate_mcp_request(b"Bearer opaque-token")

    assert get_mcp_credential_default_context() == CREDENTIAL


@pytest.mark.asyncio
async def test_the_grant_lookup_reads_the_settings_row_of_this_user_and_client(monkeypatch):
    """One lookup by ``(user_id, client_id)`` in the verify session."""
    import auth.oauth2_bearer as bearer
    import db.base

    row = SimpleNamespace(user_id="user-1", scope="memory:read", resource=None, client_id="c-1")
    statements: list = []

    class _Db:
        async def execute(self, stmt):
            statements.append(stmt)
            return MagicMock(scalar_one_or_none=MagicMock(return_value=CREDENTIAL))

    async def get_db():
        yield _Db()

    monkeypatch.setattr(db.base, "get_db", get_db)
    monkeypatch.setattr(bearer, "find_active_oauth_token", AsyncMock(return_value=row))

    grant = await mcp_auth._verify_oauth2_token("tok")

    assert grant is not None and grant.default_context_id == CREDENTIAL
    (stmt,) = statements
    sql = str(stmt)
    assert "oauth_grant_settings" in sql
    assert "user_id" in sql and "client_id" in sql
    assert set(stmt.compile().params.values()) == {"user-1", "c-1"}


@pytest.mark.asyncio
async def test_a_failed_grant_lookup_is_no_default(monkeypatch):
    import auth.oauth2_bearer as bearer
    import db.base

    row = SimpleNamespace(user_id="user-1", scope="memory:read", resource=None, client_id="c-1")

    class _Db:
        async def execute(self, _stmt):
            raise RuntimeError("db down")

    async def get_db():
        yield _Db()

    monkeypatch.setattr(db.base, "get_db", get_db)
    monkeypatch.setattr(bearer, "find_active_oauth_token", AsyncMock(return_value=row))

    grant = await mcp_auth._verify_oauth2_token("tok")

    assert grant is not None and grant.default_context_id is None


# ------------------------------------------------------------------ dispatcher


@pytest.fixture
def handlers(monkeypatch):
    received: dict[str, dict] = {}

    async def no_refusal(*_args, **_kwargs):
        return None

    def stub(name):
        async def handler(args, _user_id, _workspace_id):
            received[name] = args
            return [tools_mod.TextContent(type="text", text='{"status":"success"}')]

        return handler

    monkeypatch.setattr(capacity_gate, "capacity_lock_refusal", no_refusal)
    registry = {d["name"]: stub(d["name"]) for d in get_tool_definitions()}
    monkeypatch.setattr(tools_mod, "_TOOL_REGISTRY", registry)
    with patch.object(tools_mod, "_check_rate_limit", new=AsyncMock(return_value=(True, 0, 100))):
        yield received


def _remember_args() -> dict:
    return {"summary": "a summary long enough", "content": "c", "type": "note"}


@pytest.mark.asyncio
async def test_a_call_without_context_id_runs_in_the_credential_default(handlers):
    set_mcp_credential_default_context(CREDENTIAL)

    result = await execute_tool_call("remember", _remember_args(), "user-1", None)

    assert handlers["remember"]["context_id"] == str(CREDENTIAL)
    assert json.loads(result[0].text)["context_id"] == str(CREDENTIAL)


@pytest.mark.asyncio
async def test_an_explicit_context_id_wins_over_the_credential_default(handlers):
    explicit = str(uuid4())
    set_mcp_credential_default_context(CREDENTIAL)

    await execute_tool_call("remember", {**_remember_args(), "context_id": explicit}, "u", None)

    assert handlers["remember"]["context_id"] == explicit


@pytest.mark.asyncio
async def test_the_url_default_wins_over_the_credential_default_in_the_dispatcher(handlers):
    url = uuid4()
    set_mcp_default_context_selection(DefaultContextSelection("url", url))
    set_mcp_credential_default_context(CREDENTIAL)

    await execute_tool_call("remember", _remember_args(), "user-1", None)

    assert handlers["remember"]["context_id"] == str(url)


@pytest.mark.asyncio
async def test_an_exempt_tool_never_receives_the_credential_default(handlers):
    set_mcp_credential_default_context(CREDENTIAL)

    await execute_tool_call("delete_context", {}, "user-1", None)

    assert "context_id" not in handlers.get("delete_context", {})


@pytest.mark.asyncio
async def test_a_denied_credential_default_names_its_source(monkeypatch, handlers):
    from mcp_server.tools._helpers import _error_response

    async def denied(_args, _user_id, _workspace_id):
        return _error_response("context_not_found", "Context not found or access denied.")

    monkeypatch.setitem(tools_mod._TOOL_REGISTRY, "remember", denied)
    set_mcp_credential_default_context(CREDENTIAL)

    result = await execute_tool_call("remember", _remember_args(), "user-1", None)

    payload = json.loads(result[0].text)
    assert payload["error"] == "context_not_found"
    assert payload["context_source"] == "credential"
    assert payload["help"] == _DEFAULT_CONTEXT_DENIED_HELP["credential"]
    assert "Credentials & Integrations" in payload["help"]


# ------------------------------------------------------------------ instructions


@pytest.fixture
def reader(monkeypatch):
    import db.base as db_base
    import services.guardrail_digest as digest_mod
    from config.settings import get_settings
    from services.permission_service import PermissionService

    state = SimpleNamespace(calls=[], result=None, raise_with=None, digests=[], last_used=[])

    async def fake_fetch_entries(_db, **kwargs):
        state.digests.append(kwargs["context_id"])
        return None

    async def fake_last_used(_db, **_kwargs):
        state.last_used.append(True)
        return None

    async def fake_get_db():
        yield AsyncMock()

    async def fake_resolve(self, **kwargs):
        state.calls.append(kwargs)
        if state.raise_with is not None:
            raise state.raise_with
        return state.result

    monkeypatch.setattr(db_base, "get_db", fake_get_db)
    monkeypatch.setattr(PermissionService, "resolve_context_for_workspace_read", fake_resolve)
    monkeypatch.setattr(digest_mod, "fetch_entries", fake_fetch_entries)
    monkeypatch.setattr(digest_mod, "select_last_used_context", fake_last_used)
    monkeypatch.setattr(get_settings(), "mcp_guardrail_digest_last_used_fallback", True)
    return state


async def _instructions(query: bytes, key_workspace_id=None) -> tuple[str, bool]:
    from mcp_server.transport import build_instructions

    return await build_instructions(
        user_id="user-1", query_string=query, key_workspace_id=key_workspace_id, era="stateless"
    )


@pytest.mark.asyncio
async def test_the_credential_default_is_named_privately_through_the_read_resolver(reader):
    from mcp_server.transport import default_context_instructions
    from services.capacity_lock import CapacityPolicy

    key_ws = uuid4()
    reader.result = SimpleNamespace(name="proj-key")
    set_mcp_credential_default_context(CREDENTIAL)

    text, private = await _instructions(b"profile=core", key_workspace_id=key_ws)

    assert (text, private) == (default_context_instructions("proj-key"), True)
    (call,) = reader.calls
    assert call["context_id"] == CREDENTIAL
    assert call["key_workspace_id"] == key_ws
    assert call["required_role"] == "viewer"
    assert call["capacity"] is CapacityPolicy.ALLOWED


@pytest.mark.asyncio
async def test_the_url_default_is_named_instead_of_the_credential_default(reader):
    url = uuid4()
    reader.result = SimpleNamespace(name="proj-url")
    set_mcp_credential_default_context(CREDENTIAL)

    text, _ = await _instructions(f"context={url}".encode())

    assert "Default context: proj-url;" in text
    assert [c["context_id"] for c in reader.calls] == [url]


@pytest.mark.asyncio
async def test_a_denied_credential_default_serves_the_base_text_privately(reader):
    from mcp_server.transport import SERVER_INSTRUCTIONS_BASE
    from utils.exceptions import NotFoundException

    reader.raise_with = NotFoundException("Context", "x")
    set_mcp_credential_default_context(CREDENTIAL)

    assert await _instructions(b"") == (SERVER_INSTRUCTIONS_BASE, True)


@pytest.mark.asyncio
async def test_two_defaults_of_one_user_get_different_texts(reader):
    """No server-side cache keyed by user may serve one key's text for the other."""
    names = {}

    async def by_id(self, **kwargs):
        return SimpleNamespace(name=names[kwargs["context_id"]])

    from services.permission_service import PermissionService

    a, b = uuid4(), uuid4()
    names.update({a: "alpha", b: "beta"})
    with patch.object(PermissionService, "resolve_context_for_workspace_read", by_id):
        set_mcp_credential_default_context(a)
        first, _ = await _instructions(b"")
        set_mcp_credential_default_context(b)
        second, _ = await _instructions(b"")

    assert "Default context: alpha;" in first
    assert "Default context: beta;" in second


@pytest.mark.asyncio
async def test_the_digest_follows_the_credential_default_before_last_used(reader):
    reader.result = SimpleNamespace(name="proj-key")
    set_mcp_credential_default_context(CREDENTIAL)

    await _instructions(b"")

    assert reader.digests == [CREDENTIAL]
    assert reader.last_used == []


@pytest.mark.asyncio
async def test_without_a_credential_default_the_digest_uses_last_used(reader):
    await _instructions(b"")

    assert reader.last_used == [True]


@pytest.mark.asyncio
async def test_an_explicit_guardrails_value_still_picks_the_digest(reader):
    other = uuid4()
    reader.result = SimpleNamespace(name="proj-key")
    set_mcp_credential_default_context(CREDENTIAL)

    await _instructions(f"guardrails={other}".encode())

    assert reader.digests == [other]
