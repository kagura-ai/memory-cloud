"""What the REST public search runs for every caller (#3035).

``POST /api/v1/public/{context_id}/search``, driven directly with a fake
session that records the order of its calls:

- reranking is closed for every caller (``use_rerank`` is accepted, ignored
  and deprecated in the schema);
- a bound key's use is committed right after the context check, before the
  workspace is loaded; a key refused at attribution or by the context check
  commits nothing, and the anonymous path gains no commit;
- each row is answered once, and only the context's live rows: the
  search fetches twice ``limit``, repeats are dropped, one read in a
  savepoint keeps the rows of the context that are not deleted, the list is
  cut to ``limit``; when that read fails the de-duplicated results are
  served unfiltered;
- the four rate-limit refusals say which limit refused, and a search the
  workspace cannot run for now answers 503 without a reason;
- the completed and failed lines carry the stage timings, the embedding
  cache hit and the fusion.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import structlog
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


# ---------------------------------------------------------------------------
# Each row once, only the context's live rows
# ---------------------------------------------------------------------------


def _hit(memory_id: str, *, point_id: str | None = None, score: float = 0.5) -> dict[str, Any]:
    """A search hit: a resource point carries the row id in its payload."""
    payload: dict[str, Any] = {"summary": f"s-{memory_id[:6]}"}
    if point_id is not None:
        payload["memory_id"] = memory_id
    return {"id": point_id or memory_id, "score": score, "payload": payload}


def _row(memory_id: str, doc_id: str | None = None) -> SimpleNamespace:
    from uuid import UUID

    return SimpleNamespace(id=UUID(memory_id), resource_doc_id=doc_id)


def _ids(response: Any) -> list[str]:
    return [r.memory_id for r in response.results]


def _event(logs: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [entry for entry in logs if entry["event"] == name]
    assert len(matches) == 1, f"expected one {name!r}, got {logs!r}"
    return matches[0]


def _caller(rig: _Rig, bound: bool) -> None:
    if bound:
        rig.bound()


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_the_search_fetches_twice_the_limit(bound: bool) -> None:
    rig = _Rig()
    _caller(rig, bound)
    await rig.run(limit=7)
    assert rig.search_kwargs["k"] == 14


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_a_repeated_row_is_answered_once_at_its_first_position(bound: bool) -> None:
    rig = _Rig()
    _caller(rig, bound)
    a, b = str(uuid4()), str(uuid4())
    rig.hits = [_hit(a, point_id=str(uuid4())), _hit(b), _hit(a), _hit(b, point_id=str(uuid4()))]
    rig.db.rows = [_row(a), _row(b)]
    response = await rig.run(limit=5)
    assert _ids(response) == [a, b]
    assert response.count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_rows_the_read_does_not_return_are_dropped(bound: bool) -> None:
    """Another context's row, a tombstoned row: the read does not return them."""
    rig = _Rig()
    _caller(rig, bound)
    live, gone, foreign = str(uuid4()), str(uuid4()), str(uuid4())
    rig.hits = [_hit(gone), _hit(live), _hit(foreign)]
    rig.db.rows = [_row(live)]
    with structlog.testing.capture_logs() as logs:
        response = await rig.run(limit=5)
    assert _ids(response) == [live]
    assert response.count == 1
    assert _event(logs, "public_search_completed")["results_dropped"] == 2
    assert _event(logs, "public_search_completed")["result_count"] == 1


@pytest.mark.asyncio
async def test_the_read_is_one_statement_in_a_savepoint_scoped_to_the_context() -> None:
    rig = _Rig()
    a = str(uuid4())
    rig.hits = [_hit(a)]
    rig.db.rows = [_row(a)]
    await rig.run()
    i = rig.db.calls.index("savepoint")
    assert rig.db.calls[i : i + 3] == ["savepoint", "execute", "savepoint_release"]
    assert rig.db.calls.count("execute") == 1
    compiled = rig.db.statements[0].compile()
    sql = str(compiled)
    for column in (
        "memories.id IN",
        "memories.workspace_id =",
        "memories.context_id =",
        "memories.deleted_at IS NULL",
    ):
        assert column in sql
    params = compiled.params
    assert rig.context.workspace_id in params.values()
    assert rig.context.id in params.values()


@pytest.mark.asyncio
async def test_a_memory_id_that_is_not_a_uuid_is_dropped() -> None:
    rig = _Rig()
    a = str(uuid4())
    rig.hits = [{"id": "not-a-row", "score": 0.9, "payload": {}}, _hit(a)]
    rig.db.rows = [_row(a)]
    response = await rig.run()
    assert _ids(response) == [a]


@pytest.mark.asyncio
async def test_no_uuid_result_means_no_read_and_no_result() -> None:
    rig = _Rig()
    rig.hits = [{"id": "not-a-row", "score": 0.9, "payload": {}}]
    response = await rig.run()
    assert response.results == []
    assert "execute" not in rig.db.calls


@pytest.mark.asyncio
async def test_the_list_is_cut_to_the_limit_after_the_filter() -> None:
    rig = _Rig()
    ids = [str(uuid4()) for _ in range(6)]
    rig.hits = [_hit(i) for i in ids]
    rig.db.rows = [_row(i) for i in ids if i != ids[0]]
    with structlog.testing.capture_logs() as logs:
        response = await rig.run(limit=3)
    assert _ids(response) == ids[1:4]
    assert response.count == 3
    # The cut to ``limit`` is not a drop: only the filter removed one.
    assert _event(logs, "public_search_completed")["results_dropped"] == 1


@pytest.mark.asyncio
async def test_no_read_when_the_search_returned_nothing() -> None:
    rig = _Rig()
    with structlog.testing.capture_logs() as logs:
        response = await rig.run()
    assert response.results == []
    assert response.count == 0
    assert "execute" not in rig.db.calls
    assert "savepoint" not in rig.db.calls
    assert _event(logs, "public_search_completed")["results_dropped"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_a_failed_read_serves_the_de_duplicated_results_unfiltered(bound: bool) -> None:
    rig = _Rig()
    _caller(rig, bound)
    ids = [str(uuid4()) for _ in range(4)]
    rig.hits = [_hit(ids[0]), _hit(ids[0]), *(_hit(i) for i in ids[1:])]
    rig.db.execute_error = RuntimeError("connection reset: secret-dsn-text")
    with structlog.testing.capture_logs() as logs:
        response = await rig.run(limit=3)
    assert _ids(response) == ids[:3]
    assert response.status == "success"
    warning = _event(logs, "public_search_filter_unread")
    assert warning["log_level"] == "warning"
    assert set(warning) == {"event", "log_level", "error_type"}
    assert warning["error_type"] == "RuntimeError"
    assert "secret-dsn-text" not in repr(logs)
    assert "savepoint_rollback" in rig.db.calls
    # The repeat was still dropped.
    assert _event(logs, "public_search_completed")["results_dropped"] == 1


# ---------------------------------------------------------------------------
# Which limit refused
# ---------------------------------------------------------------------------


def _counter(value: int):
    async def _incr(_key: str, amount: int = 1, ttl: int = 60) -> int:
        return value

    return _incr


def _window(value: int):
    """The pre-key bucket's counter (#3054: a returnable window slot)."""
    from db.redis import WindowSlot

    async def _take(key: str, ttl: int = 60) -> WindowSlot:
        return WindowSlot(key=key, count=value, expires_at_ms=None)

    return _take


def test_a_rate_limit_error_without_a_reason_has_no_reason_key() -> None:
    from utils.exceptions import RateLimitError

    plain = RateLimitError("over", retry_after=60)
    assert plain.details == {"retry_after": 60}
    assert plain.error_code == "RATE-001"
    with_reason = RateLimitError("over", retry_after=60, reason="key_limit")
    assert with_reason.details == {"retry_after": 60, "reason": "key_limit"}
    assert with_reason.retry_after == 60


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("check", "args", "count", "reason"),
    [
        ("check_public_search_rate_limit", (uuid4(),), 51, "anonymous_limit"),
        ("check_pre_auth_rate_limit", (uuid4(),), 201, "pre_auth_limit"),
        ("check_bound_key_rate_limit", (7, 100), 101, "key_limit"),
        ("check_bound_key_rate_limit", (7, 0), 1, "key_not_provisioned"),
    ],
)
async def test_each_refusal_names_its_limit(
    check: str, args: tuple[Any, ...], count: int, reason: str
) -> None:
    from utils.exceptions import RateLimitError

    with (
        patch.object(route, "incrby_counter", _counter(count)),
        patch.object(route, "take_window_slot", _window(count)),
        pytest.raises(RateLimitError) as exc,
    ):
        await getattr(route, check)(*args)
    assert exc.value.error_code == "RATE-001"
    assert exc.value.status_code == 429
    assert exc.value.details["reason"] == reason
    assert exc.value.retry_after == 60


@pytest.mark.asyncio
async def test_the_pre_auth_refusal_names_its_limit_on_search_and_info() -> None:
    from api.routes.public_search import get_public_context_info
    from utils.exceptions import RateLimitError

    context_id = uuid4()
    for call in (
        lambda: public_search(
            context_id=context_id,
            request=PublicSearchRequest(query="q"),
            api_key="kagura_x",
            db=MagicMock(),
        ),
        lambda: get_public_context_info(context_id=context_id, api_key="kagura_x", db=MagicMock()),
    ):
        with (
            patch.object(route, "take_window_slot", _window(201)),
            pytest.raises(RateLimitError) as exc,
        ):
            await call()
        assert exc.value.details["reason"] == "pre_auth_limit"


@pytest.mark.asyncio
async def test_the_reason_reaches_the_client_body() -> None:
    import json

    from api.main import memory_cloud_exception_handler
    from utils.exceptions import RateLimitError

    response = await memory_cloud_exception_handler(
        MagicMock(url=SimpleNamespace(path="/api/v1/public/x/search")),
        RateLimitError("over", retry_after=60, reason="anonymous_limit"),
    )
    body = json.loads(bytes(response.body))
    assert response.status_code == 429
    assert body["error"] == "RATE-001"
    assert body["details"] == {"retry_after": 60, "reason": "anonymous_limit"}


# ---------------------------------------------------------------------------
# A search the workspace cannot run for now is a 503, without a reason
# ---------------------------------------------------------------------------


def _spend_cap() -> Exception:
    from utils.exceptions import EmbeddingSpendCapExceeded

    return EmbeddingSpendCapExceeded(
        "Daily embedding spend cap reached ($1.2345/day)",
        period="daily",
        cap_usd=1.2345,
        current_usd=6.789,
        workspace_id=str(uuid4()),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_the_spend_stop_is_a_503_without_a_reason_or_figure(bound: bool) -> None:
    import json

    from api.main import memory_cloud_exception_handler
    from utils.exceptions import TemporarilyUnavailableError

    rig = _Rig()
    _caller(rig, bound)
    rig.search.return_value.hybrid_search = AsyncMock(side_effect=_spend_cap())
    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(TemporarilyUnavailableError) as exc,
    ):
        await rig.run()
    err = exc.value
    assert err.status_code == 503
    assert err.error_code == "temporarily_unavailable"
    assert err.details == {}
    assert err.response_headers == {"Retry-After": "300"}
    assert err.__cause__ is None and err.__suppress_context__ is True
    assert rig.log_usage.await_args.kwargs["status_code"] == 503
    failed = _event(logs, "public_search_failed")
    assert failed["reason"] == "embedding_spend_cap"
    assert failed["log_level"] == "error"

    with structlog.testing.capture_logs() as handler_logs:
        response = await memory_cloud_exception_handler(
            MagicMock(url=SimpleNamespace(path="/api/v1/public/x/search")), err
        )
    body = bytes(response.body).decode()
    assert json.loads(body)["details"] == {}
    assert response.headers["retry-after"] == "300"
    everything = body + json.dumps(logs, default=str) + json.dumps(handler_logs, default=str)
    for figure in ("1.2345", "6.789", "spend cap", "QUOTA-002"):
        assert figure not in everything


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["content_size", "plain"])
async def test_other_failures_keep_the_500(error: str) -> None:
    from utils.exceptions import ContentSizeExceededError

    rig = _Rig()
    raised = (
        ContentSizeExceededError(actual=10, limit=5) if error == "content_size" else RuntimeError()
    )
    rig.search.return_value.hybrid_search = AsyncMock(side_effect=raised)
    with structlog.testing.capture_logs() as logs, pytest.raises(HTTPException) as exc:
        await rig.run()
    assert exc.value.status_code == 500
    assert exc.value.__cause__ is raised
    assert rig.log_usage.await_args.kwargs["status_code"] == 500
    assert "reason" not in _event(logs, "public_search_failed")


# ---------------------------------------------------------------------------
# Stage timings and the embedding-cache hit on both lines
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _timed_search(clock: _Clock, seen: list[Any], *, fail: Exception | None = None):
    async def _search(**kwargs: Any) -> list[dict[str, Any]]:
        timings = kwargs["timings"]
        seen.append(timings)
        timings.fusion = "rrf"
        with timings.stage("embed"):
            clock.now += 0.012
        timings.embed_cache_hit = True
        with timings.stage("qdrant"):
            clock.now += 0.030
        clock.now += 0.005
        if fail is not None:
            raise fail
        return []

    return _search


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_the_completed_line_carries_the_stage_timings(bound: bool) -> None:
    from services.recall_timings import RecallTimings

    rig = _Rig()
    _caller(rig, bound)
    clock, seen = _Clock(), []
    rig.search.return_value.hybrid_search = AsyncMock(side_effect=_timed_search(clock, seen))
    with (
        patch.object(route, "RecallTimings", lambda: RecallTimings(clock=clock)),
        structlog.testing.capture_logs() as logs,
    ):
        await rig.run()
    assert len(seen) == 1 and rig.search_kwargs["timings"] is seen[0]
    line = _event(logs, "public_search_completed")
    assert line["duration_ms"] == 47
    assert line["embed_ms"] == 12
    assert line["qdrant_ms"] == 30
    assert line["other_ms"] == 5
    assert "rerank_ms" not in line
    assert line["embed_cache_hit"] is True
    assert line["fusion"] == "rrf"
    assert "query" not in line


@pytest.mark.asyncio
@pytest.mark.parametrize("cannot_run", [False, True])
async def test_the_failed_line_carries_the_stage_timings(cannot_run: bool) -> None:
    from services.recall_timings import RecallTimings

    rig = _Rig()
    clock, seen = _Clock(), []
    failure = _spend_cap() if cannot_run else RuntimeError("boom")
    rig.search.return_value.hybrid_search = AsyncMock(
        side_effect=_timed_search(clock, seen, fail=failure)
    )
    with (
        patch.object(route, "RecallTimings", lambda: RecallTimings(clock=clock)),
        structlog.testing.capture_logs() as logs,
        pytest.raises(Exception),  # noqa: B017 - the 500 or the 503, both pinned above
    ):
        await rig.run()
    line = _event(logs, "public_search_failed")
    assert line["duration_ms"] == 47
    assert line["embed_ms"] == 12
    assert line["qdrant_ms"] == 30
    assert line["embed_cache_hit"] is True
    assert line["fusion"] == "rrf"


@pytest.mark.asyncio
async def test_a_search_that_never_ran_logs_no_fusion() -> None:
    rig = _Rig()
    with structlog.testing.capture_logs() as logs:
        await rig.run()
    line = _event(logs, "public_search_completed")
    assert line["fusion"] is None
    assert "duration_ms" in line and "other_ms" in line
    assert "embed_cache_hit" not in line
