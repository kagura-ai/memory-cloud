"""What the REST public search runs for every caller (#3035).

``POST /api/v1/public/{context_id}/search``, driven directly with a fake
session that records the order of its calls:

- reranking is closed for every caller (``use_rerank`` is accepted, ignored
  and deprecated in the schema);
- a bound key's use is committed right after the context check, before the
  workspace is loaded; a key refused at attribution or by the context check
  commits nothing, and the anonymous path gains no commit.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.routes import public_search as route
from api.routes.public_search import PublicSearchRequest, public_search
from models.auth import Context, Workspace
from utils.exceptions import APIKeyError, AuthorizationError, NotFoundException

OWNER_ID = "owner-user"


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _Nested:
    def __init__(self, db: _FakeDB) -> None:
        self._db = db

    async def __aenter__(self) -> _Nested:
        self._db.calls.append("savepoint")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._db.calls.append("savepoint_rollback" if exc_type else "savepoint_release")
        return False


class _FakeDB:
    """A request session: ``get``, ``commit``, ``begin_nested`` and ``execute``."""

    def __init__(self, *, context: Any, workspace: Any) -> None:
        self.context = context
        self.workspace = workspace
        self.calls: list[str] = []
        self.rows: list[Any] = []
        self.execute_error: Exception | None = None
        self.statements: list[Any] = []

    async def get(self, model: type, _id: Any) -> Any:
        self.calls.append(f"get:{model.__name__}")
        return {Context: self.context, Workspace: self.workspace}.get(model)

    async def commit(self) -> None:
        self.calls.append("commit")

    async def rollback(self) -> None:
        self.calls.append("rollback")

    def begin_nested(self) -> _Nested:
        return _Nested(self)

    async def execute(self, statement: Any, *_a: Any, **_k: Any) -> _Result:
        self.calls.append("execute")
        self.statements.append(statement)
        if self.execute_error is not None:
            raise self.execute_error
        return _Result(self.rows)


def _context(**overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "is_public": True,
        "resource_id": None,
        "name": "pub",
        "deleted_at": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _Rig:
    """The route with every gate and service patched; only the db is real-ish."""

    def __init__(self, *, plan: str = "pro", context: Any = None) -> None:
        self.context = context if context is not None else _context()
        self.workspace = Workspace(
            id=self.context.workspace_id, name="w", plan_name=plan, owner_user_id=OWNER_ID
        )
        self.db = _FakeDB(context=self.context, workspace=self.workspace)
        self.hits: list[dict[str, Any]] = []
        self.search = MagicMock()
        self.search.return_value.hybrid_search = AsyncMock(side_effect=self._search)
        self.log_usage = AsyncMock()
        self.attribution = AsyncMock(return_value=None)
        self.bound_bucket = AsyncMock()
        self.anon_bucket = AsyncMock(return_value=True)

    async def _search(self, **_kwargs: Any) -> list[dict[str, Any]]:
        self.db.calls.append("search")
        return list(self.hits)

    def bound(self, key_id: int = 7) -> SimpleNamespace:
        key = SimpleNamespace(id=key_id, user_id=OWNER_ID, bound_context_id=self.context.id)
        self.attribution.return_value = key
        return key

    async def run(self, *, limit: int = 3, use_rerank: bool = False, api_key: str | None = None):
        if self.attribution.return_value is not None and api_key is None:
            api_key = "kagura_bound"
        with (
            patch.object(route, "check_pre_auth_rate_limit", AsyncMock()),
            patch.object(route, "_resolve_public_attribution", self.attribution),
            patch.object(route, "check_bound_key_rate_limit", self.bound_bucket),
            patch.object(route, "check_public_search_rate_limit", self.anon_bucket),
            patch.object(route, "public_usage_budget_limit", lambda: None),
            patch.object(route, "ensure_not_capacity_locked", AsyncMock()),
            patch.object(route, "ensure_slot_active", AsyncMock()),
            patch.object(route, "SearchService", self.search),
            patch.object(route, "log_usage", self.log_usage),
        ):
            return await public_search(
                context_id=self.context.id,
                request=PublicSearchRequest(query="q", limit=limit, use_rerank=use_rerank),
                api_key=api_key,
                db=self.db,  # type: ignore[arg-type]
            )

    @property
    def search_kwargs(self) -> dict[str, Any]:
        return self.search.return_value.hybrid_search.await_args.kwargs


# ---------------------------------------------------------------------------
# Reranking is closed for every caller
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_no_caller_reranks_even_when_it_asks(bound: bool) -> None:
    rig = _Rig()
    if bound:
        rig.bound()
    response = await rig.run(use_rerank=True)
    assert response.status == "success"
    assert rig.search_kwargs["use_rerank"] is False


def test_use_rerank_is_deprecated_and_ignored_in_the_schema() -> None:
    field = PublicSearchRequest.model_fields["use_rerank"]
    assert field.default is False
    assert field.deprecated is True
    assert field.description == "Ignored: the public search never reranks."
    # A client that still sends it is not refused.
    assert PublicSearchRequest(query="q", use_rerank=True).use_rerank is True


# ---------------------------------------------------------------------------
# A bound key's use is committed after the context check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_bound_key_commits_after_the_context_and_before_the_workspace() -> None:
    rig = _Rig()
    rig.bound()
    await rig.run()
    calls = rig.db.calls
    assert calls.count("commit") == 1
    assert calls.index("get:Context") < calls.index("commit") < calls.index("get:Workspace")
    assert calls.index("commit") < calls.index("search")


@pytest.mark.asyncio
async def test_a_bound_key_refused_by_its_own_bucket_keeps_the_commit() -> None:
    """The key was used: its last-used time stays committed (documented)."""
    from utils.exceptions import RateLimitError

    rig = _Rig()
    rig.bound()
    rig.bound_bucket.side_effect = RateLimitError("over", retry_after=60)
    with pytest.raises(RateLimitError):
        await rig.run()
    assert rig.db.calls.count("commit") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("context", "error"),
    [
        (None, NotFoundException),
        (_context(is_public=False), AuthorizationError),
    ],
)
async def test_a_bound_key_refused_by_the_context_check_commits_nothing(
    context: Any, error: type[Exception]
) -> None:
    rig = _Rig()
    rig.bound()
    rig.db.context = context
    with pytest.raises(error):
        await rig.run()
    assert "commit" not in rig.db.calls


@pytest.mark.asyncio
async def test_a_key_refused_at_attribution_commits_nothing() -> None:
    rig = _Rig()
    rig.attribution.side_effect = APIKeyError("Invalid or expired API key")
    with pytest.raises(APIKeyError):
        await rig.run(api_key="kagura_stale")
    assert rig.db.calls == []


@pytest.mark.asyncio
async def test_the_anonymous_path_gains_no_commit() -> None:
    rig = _Rig()
    await rig.run()
    assert "commit" not in rig.db.calls


@pytest.mark.asyncio
async def test_a_missing_workspace_after_the_commit_is_still_the_500() -> None:
    rig = _Rig()
    rig.bound()
    rig.db.workspace = None
    with pytest.raises(HTTPException) as exc:
        await rig.run()
    assert exc.value.status_code == 500
    assert rig.db.calls.count("commit") == 1
