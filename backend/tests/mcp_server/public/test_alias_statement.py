"""One statement for both entries of a public MCP endpoint (#2809).

The fixed address (``/mcp/public/{pmcp_id}``) and a verified alias host are
loaded by one SELECT, ``_serving_select()``: the alias adds the join to its
domain row (same endpoint, same workspace), the active state and the lease,
and nothing else. The lease predicate does not depend on the authority
setting. No database: the statements are compiled and a fake session
returns scripted rows.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
import structlog
from sqlalchemy.dialects import postgresql

from config.settings import get_settings
from mcp_server.public import publication
from mcp_server.public.publication import (
    ServingEndpoint,
    load_serving_endpoint,
    load_serving_endpoint_for_alias,
)

ALIAS = "mcp.customer.example.org"
PUBLIC_ID = f"pmcp_{'A' * 22}"


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Db:
    """Records every statement; answers each with ``row``."""

    def __init__(self, row=None):
        self.row = row
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.row)


class _NoDb:
    async def execute(self, statement):  # pragma: no cover - must not run
        raise AssertionError("no statement may run")


def _sql(statement) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def _split(sql: str) -> tuple[str, str]:
    head, _, where = sql.partition("\nWHERE ")
    assert where, sql
    return head, where


@pytest.fixture
def switches_on(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "mcp_public_endpoints_enabled", True)
    monkeypatch.setattr(s, "mcp_public_domains_enabled", True)
    return monkeypatch


async def _statements(monkeypatch) -> tuple[str, str]:
    fixed_db, alias_db = _Db(), _Db()
    assert await load_serving_endpoint(fixed_db, PUBLIC_ID) is None
    assert await load_serving_endpoint_for_alias(alias_db, ALIAS) is None
    assert len(fixed_db.statements) == 1 and len(alias_db.statements) == 1
    return _sql(fixed_db.statements[0]), _sql(alias_db.statements[0])


async def test_both_loaders_are_built_from_the_one_select(switches_on) -> None:
    calls: list[int] = []
    real = publication._serving_select

    def spy():
        calls.append(1)
        return real()

    switches_on.setattr(publication, "_serving_select", spy)
    await _statements(switches_on)
    assert len(calls) == 2


async def test_alias_statement_holds_every_predicate_of_the_fixed_one(switches_on) -> None:
    fixed, alias = await _statements(switches_on)
    base_head, base_where = _split(_sql(publication._serving_select()))
    fixed_head, fixed_where = _split(fixed)
    alias_head, alias_where = _split(alias)

    # Same SELECT list and joins; the alias adds exactly its domain join.
    assert fixed_head == base_head
    assert alias_head.startswith(base_head)
    assert alias_head[len(base_head) :].strip() == (
        "JOIN public_mcp_domains ON public_mcp_domains.endpoint_id = public_mcp_endpoints.id"
        " AND public_mcp_domains.workspace_id = public_mcp_endpoints.workspace_id"
    )
    # Every predicate of the shared select is in both statements.
    for predicate in base_where.split(" AND "):
        assert predicate in fixed_where, predicate
        assert predicate in alias_where, predicate
    for predicate in (
        "public_mcp_endpoints.revoked_at IS NULL",
        "contexts.deleted_at IS NULL",
        "contexts.is_public IS true",
        "contexts.is_private IS false",
        "workspaces.deleted_at IS NULL",
    ):
        assert predicate in base_where, predicate
    # The joins bind the context and the resource to the endpoint's workspace.
    assert "contexts.workspace_id = public_mcp_endpoints.workspace_id" in base_head
    assert "resources.workspace_id = public_mcp_endpoints.workspace_id" in base_head
    assert "JOIN workspaces ON workspaces.id = public_mcp_endpoints.workspace_id" in base_head

    # Each entry adds only its own address.
    assert fixed_where == base_where + f" AND public_mcp_endpoints.public_id = '{PUBLIC_ID}'"
    assert "public_id" not in alias_where
    assert alias_where == base_where + (
        f" AND public_mcp_domains.hostname = '{ALIAS}'"
        " AND public_mcp_domains.state = 'active'"
        " AND public_mcp_domains.serve_until > timezone('UTC', statement_timestamp())"
    )


@pytest.mark.parametrize("authority", ["entitlement", "operator"])
async def test_lease_predicate_under_both_authority_values(switches_on, authority) -> None:
    switches_on.setattr(get_settings(), "mcp_public_domains_authority", authority)
    _, alias = await _statements(switches_on)
    assert "public_mcp_domains.serve_until > timezone('UTC', statement_timestamp())" in alias
    assert "public_mcp_domains.state = 'active'" in alias


async def test_alias_statement_does_not_depend_on_the_authority(switches_on) -> None:
    switches_on.setattr(get_settings(), "mcp_public_domains_authority", "entitlement")
    _, under_entitlement = await _statements(switches_on)
    switches_on.setattr(get_settings(), "mcp_public_domains_authority", "operator")
    _, under_operator = await _statements(switches_on)
    assert under_entitlement == under_operator


@pytest.mark.parametrize(
    ("endpoints", "domains_on"), [(False, True), (True, False), (False, False)]
)
async def test_alias_loader_runs_nothing_unless_both_switches_are_on(
    monkeypatch, endpoints: bool, domains_on: bool
) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "mcp_public_endpoints_enabled", endpoints)
    monkeypatch.setattr(s, "mcp_public_domains_enabled", domains_on)
    assert await load_serving_endpoint_for_alias(_NoDb(), ALIAS) is None


async def test_fixed_loader_ignores_the_domains_switch(monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "mcp_public_endpoints_enabled", True)
    monkeypatch.setattr(s, "mcp_public_domains_enabled", False)
    db = _Db()
    assert await load_serving_endpoint(db, PUBLIC_ID) is None
    assert len(db.statements) == 1
    assert "public_mcp_domains" not in _sql(db.statements[0])
    monkeypatch.setattr(s, "mcp_public_endpoints_enabled", False)
    assert await load_serving_endpoint(_NoDb(), PUBLIC_ID) is None


def _row():
    workspace = SimpleNamespace(id=uuid4(), owner_user_id="owner-1")
    endpoint = SimpleNamespace(
        id=uuid4(),
        public_id=PUBLIC_ID,
        workspace_id=workspace.id,
        context_id=uuid4(),
        resource_pk=uuid4(),
        resource_id="catalog",
        public_name="Catalog",
        public_description="d",
        public_usage_guide=None,
    )
    return (endpoint, "catalog-context", workspace)


async def test_both_entries_load_the_same_serving_endpoint(switches_on) -> None:
    row = _row()
    fixed = await load_serving_endpoint(_Db(row), PUBLIC_ID)
    alias = await load_serving_endpoint_for_alias(_Db(row), ALIAS)
    assert isinstance(fixed, ServingEndpoint)
    assert fixed == alias
    assert alias is not None and alias.id == row[0].id
    assert alias.workspace is row[2]
    assert alias.owner_user_id == "owner-1"
    assert alias.context_name == "catalog-context"


async def test_an_unserved_alias_logs_no_host_name(switches_on) -> None:
    with structlog.testing.capture_logs() as logs:
        assert await load_serving_endpoint_for_alias(_Db(), ALIAS) is None
    assert ALIAS not in repr(logs)
