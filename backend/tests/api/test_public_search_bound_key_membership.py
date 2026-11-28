"""The REST public search refuses a bound key whose owner does not hold its workspace.

Unit level, on stubs (the database cases are in
``tests/integration/test_bound_key_membership_db.py``):

* a public-bound key whose owner does not hold the live workspace of its
  context is refused with an unknown key's error, before the scope checks;
* the membership statement is one EXISTS over the context, its live workspace
  and the membership row; it runs only for a bound key;
* a soft-deleted context, and a context of a soft-deleted workspace, raise the
  missing context's 404 on ``/search`` and ``/info``;
* the member credentials listing and the member's removal use one predicate.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Delete

import api.routes.public_search as route
from auth.api_keys import VerifiedKey, bound_key_owner_holds_context_workspace
from models.auth import Context, Workspace
from utils.datetime import utcnow
from utils.exceptions import APIKeyError, AuthorizationError, NotFoundException


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _bound(context_id) -> VerifiedKey:
    return VerifiedKey(
        id=7, user_id="u-1", workspace_id=None, bound_context_id=context_id, key_prefix="kagura_abc"
    )


async def _resolve(verified, *, holds: bool, context_id=None):
    manager = MagicMock()
    manager.return_value.verify_key = AsyncMock(return_value=verified)
    check = AsyncMock(return_value=holds)
    with (
        patch.object(route, "APIKeyManager", manager),
        patch.object(route, "bound_key_owner_holds_context_workspace", check),
    ):
        try:
            return await route._resolve_public_attribution(
                "kagura_x", context_id or verified.bound_context_id, MagicMock()
            ), check
        except Exception as exc:  # noqa: BLE001 - returned for the assertions
            return exc, check


# ---------------------------------------------------------------------------
# _resolve_public_attribution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_bound_key_of_a_departed_owner_reads_as_an_unknown_key():
    context_id = uuid4()
    refused, check = await _resolve(_bound(context_id), holds=False)
    unknown, _ = await _resolve(None, holds=True, context_id=context_id)

    assert isinstance(refused, APIKeyError) and isinstance(unknown, APIKeyError)
    assert (refused.status_code, refused.error_code, refused.message) == (
        unknown.status_code,
        unknown.error_code,
        unknown.message,
    )
    assert refused.details == unknown.details == {}
    check.assert_awaited_once()
    assert check.await_args.kwargs == {"user_id": "u-1", "context_id": context_id}


@pytest.mark.asyncio
async def test_the_membership_check_runs_before_the_scope_check():
    """A departed owner's key on another context is a 401, not the 403 scope
    violation: the key is dead whatever it is used for."""
    refused, _ = await _resolve(_bound(uuid4()), holds=False, context_id=uuid4())

    assert isinstance(refused, APIKeyError)


@pytest.mark.asyncio
async def test_a_current_members_bound_key_resolves():
    verified = _bound(uuid4())
    resolved, check = await _resolve(verified, holds=True)

    assert resolved is verified
    check.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_key_without_a_binding_runs_no_membership_check():
    verified = VerifiedKey(id=8, user_id="u-1", workspace_id=uuid4(), bound_context_id=None)
    refused, check = await _resolve(verified, holds=False, context_id=uuid4())

    assert isinstance(refused, AuthorizationError)
    check.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_membership_statement_is_one_exists_over_the_live_workspace():
    db = MagicMock()
    db.scalar = AsyncMock(return_value=True)

    assert await bound_key_owner_holds_context_workspace(db, user_id="u-1", context_id=uuid4())

    (stmt,), _ = db.scalar.await_args
    sql = " ".join(_compiled(stmt).split())
    assert sql.startswith("SELECT EXISTS (SELECT")
    assert "contexts_1.id = " in sql
    assert "workspaces_1.id = contexts_1.workspace_id" in sql
    assert "workspaces_1.deleted_at IS NULL" in sql
    assert "workspaces_1.owner_user_id = " in sql
    assert "workspace_members.workspace_id = contexts_1.workspace_id" in sql
    assert "workspace_members.user_id = " in sql


def test_the_condition_correlates_to_an_outer_query_over_api_keys():
    """MCP binding introspection filters ``api_keys`` rows by the same rule:
    both levels of the EXISTS read the outer row's owner and binding."""
    from sqlalchemy import select

    from auth.api_keys import bound_key_owner_holds
    from models.auth import APIKey

    stmt = (
        select(APIKey.id, Context.name)
        .outerjoin(Context, Context.id == APIKey.bound_context_id)
        .where(bound_key_owner_holds(APIKey.user_id, APIKey.bound_context_id))
    )
    sql = " ".join(_compiled(stmt).split())

    assert sql.count("api_keys AS") == 0
    assert sql.count("FROM api_keys") == 1
    assert "contexts_1.id = api_keys.bound_context_id" in sql
    assert "workspaces_1.owner_user_id = api_keys.user_id" in sql
    assert "FROM workspace_members WHERE" in sql
    assert "workspace_members.user_id = api_keys.user_id" in sql


# ---------------------------------------------------------------------------
# Soft-deleted context or workspace
# ---------------------------------------------------------------------------


def _db(*, context_deleted: bool = False, workspace_deleted: bool = False):
    workspace_id = uuid4()
    context = SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id,
        is_public=True,
        resource_id=None,
        name="pub",
        display_name=None,
        description=None,
        created_at=None,
        deleted_at=utcnow() if context_deleted else None,
    )
    workspace = Workspace(id=workspace_id, name="w", plan_name="pro", owner_user_id="owner")
    workspace.deleted_at = utcnow() if workspace_deleted else None

    async def _get(model, _id):
        return {Context: context, Workspace: workspace}[model]

    db = MagicMock()
    db.get = AsyncMock(side_effect=_get)
    return db, context


async def _call(which: str, db, context):
    with (
        patch.object(route, "check_public_search_rate_limit", new=AsyncMock(return_value=True)),
        patch.object(route, "SearchService") as search,
        patch.object(route, "log_usage", new=AsyncMock()),
    ):
        search.return_value.hybrid_search = AsyncMock(return_value=[])
        if which == "search":
            return await route.public_search(
                context_id=context.id,
                request=route.PublicSearchRequest(query="q", limit=3),
                api_key=None,
                db=db,
            )
        return await route.get_public_context_info(context_id=context.id, api_key=None, db=db)


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["search", "info"])
@pytest.mark.parametrize("deleted", ["context", "workspace"])
async def test_a_soft_deleted_context_is_a_missing_one(which, deleted):
    db, context = _db(
        context_deleted=deleted == "context", workspace_deleted=deleted == "workspace"
    )

    with pytest.raises(NotFoundException) as exc:
        await _call(which, db, context)

    missing = NotFoundException("Context", str(context.id))
    assert (exc.value.status_code, exc.value.message) == (404, missing.message)


# ---------------------------------------------------------------------------
# Member credentials: one predicate for the listing and the removal
# ---------------------------------------------------------------------------


def _where(stmt) -> str:
    return " ".join(_compiled(stmt.whereclause).split())


@pytest.mark.asyncio
async def test_the_removal_deletes_by_the_listings_predicate():
    from services.member_credentials_service import MemberCredentialsService

    seen: list = []

    def _execute(stmt, *a, **k):
        seen.append(stmt)
        result = MagicMock(rowcount=0)
        result.scalars.return_value.all.return_value = []
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    workspace_id = uuid4()
    service = MemberCredentialsService(db)
    with (
        patch.object(service, "_check_can_view", new=AsyncMock()),
        patch(
            "services.member_credentials_service.default_context_names",
            new=AsyncMock(return_value={}),
        ),
    ):
        await service.get_or_create_credentials(workspace_id, "member-1", "member-1")
    with patch(
        "services.member_credentials_service.settle_departing_credentials",
        new=AsyncMock(return_value=MagicMock(counts=lambda: {})),
    ):
        await service.cleanup_member_credentials(workspace_id, "member-1")

    listing = seen[0]
    removal = next(s for s in seen[1:] if isinstance(s, Delete) and s.table.name == "api_keys")
    removal_where = _where(removal)
    assert "api_keys.bound_context_id IN (SELECT contexts.id FROM contexts" in removal_where
    assert "api_keys.workspace_id = " in removal_where
    # The listing adds only the revoked filter to the removal's predicate.
    assert _where(listing) == f"{removal_where} AND api_keys.revoked_at IS NULL"
