"""REST public searches made with a bound key, in the usage events (#3035).

The route driven directly, with a spy usage writer installed as the process's
writer, the switches set on the cached settings, the publication lookup and
the buckets patched, and a fake session that can answer the result filter.

- Recording runs only for a bound key, with both switches on, a writer in the
  process and a plan that gives bound keys an allowance; then exactly one
  event per request, whatever the exit; a serving gate's refusal leaves none.
- An anonymous caller is never recorded and its visitor header never read.
- The visitor header: one well-formed value becomes the daily key of the
  route's start day; anything else is ignored (``repeated`` / ``format``).
- Neither the key nor the visitor value reaches a log line, a repr, the
  offered event or the response.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import structlog
from fastapi import HTTPException

import services.public_mcp_usage_writer as writer_module
from api.routes import public_search as route
from api.routes.public_search import PublicSearchRequest, public_search
from config.settings import get_settings
from mcp_server.public.publication import ServingEndpoint
from models.auth import Context, Workspace
from services import public_search_usage
from services.public_mcp_usage_events import (
    UsageEventInput,
    build_usage_event_row,
    visitor_key,
)
from services.public_search_usage import VisitorHeaderSource, visitor_header_source
from utils.exceptions import (
    CapacityLockedError,
    EmbeddingSpendCapExceeded,
    PublicUsageBudgetExhaustedError,
    PublicUsageBudgetUnavailableError,
    QuotaExceededError,
    RateLimitError,
    TemporarilyUnavailableError,
    WorkspaceSlotSuspendedError,
)

OWNER_ID = "owner-user"
TOKEN_MARKER = "kagura_q7Wm2xLp9RtZ4vNbTOKENMARKER3035aa81Kf"
VISITOR_MARKER = "vis-MARKER-3035"


# ---------------------------------------------------------------------------
# Rig
# ---------------------------------------------------------------------------


class _Writer:
    def __init__(self) -> None:
        self.offered: list[UsageEventInput] = []
        self.error: Exception | None = None

    def offer(self, event: UsageEventInput) -> None:
        if self.error is not None:
            raise self.error
        self.offered.append(event)


class _Headers:
    def __init__(self, values: list[str]) -> None:
        self._values = values
        self.reads = 0

    def getlist(self, name: str) -> list[str]:
        assert name == "kagura-visitor"
        self.reads += 1
        return list(self._values)

    def get(self, name: str, default: Any = None) -> Any:
        raise AssertionError(f"read header {name}")


class _Request:
    """Only ``headers.getlist('kagura-visitor')`` may be read."""

    def __init__(self, values: list[str]) -> None:
        self.headers = _Headers(values)

    @property
    def state(self) -> Any:
        raise AssertionError("read request.state")

    @property
    def session(self) -> Any:
        raise AssertionError("read request.session")

    @property
    def cookies(self) -> Any:
        raise AssertionError("read request.cookies")


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _Nested:
    async def __aenter__(self) -> _Nested:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _DB:
    def __init__(self, context: Any, workspace: Any) -> None:
        self.context = context
        self.workspace = workspace
        self.rows: list[Any] = []
        self.execute_error: Exception | None = None
        self.commits = 0

    async def get(self, model: type, _id: Any) -> Any:
        return {Context: self.context, Workspace: self.workspace}.get(model)

    async def commit(self) -> None:
        self.commits += 1

    def begin_nested(self) -> _Nested:
        return _Nested()

    async def execute(self, statement: Any, *_a: Any, **_k: Any) -> _Result:
        if self.execute_error is not None:
            raise self.execute_error
        return _Result(self.rows)


def _row(memory_id: str, doc_id: str | None, served: bool = True) -> SimpleNamespace:
    return SimpleNamespace(id=UUID(memory_id), resource_doc_id=doc_id, in_publication=served)


def _hit(memory_id: str) -> dict[str, Any]:
    return {"id": memory_id, "score": 0.5, "payload": {"summary": "s"}}


@pytest.fixture
def switches(monkeypatch):
    """Both switches on and a spy writer as the process's writer."""
    writer = _Writer()
    monkeypatch.setattr(get_settings(), "public_search_telemetry_enabled", True)
    monkeypatch.setattr(get_settings(), "mcp_public_telemetry_enabled", True)
    monkeypatch.setattr(writer_module, "_writer", writer)
    return writer


class _Rig:
    def __init__(self, *, plan: str = "pro", visitor: list[str] | None = None) -> None:
        workspace_id = uuid4()
        self.context = SimpleNamespace(
            id=uuid4(),
            workspace_id=workspace_id,
            is_public=True,
            resource_id=None,
            name="pub",
            deleted_at=None,
        )
        self.workspace = Workspace(
            id=workspace_id, name="w", plan_name=plan, owner_user_id=OWNER_ID
        )
        self.db = _DB(self.context, self.workspace)
        self.publication: ServingEndpoint | None = ServingEndpoint(
            id=uuid4(),
            public_id="pmcp_test",
            workspace_id=workspace_id,
            context_id=self.context.id,
            resource_pk=uuid4(),
            resource_id="catalog",
            public_name="Catalog",
            public_description=None,
            public_usage_guide=None,
            context_name="pub",
            owner_user_id=OWNER_ID,
        )
        self.lookup = AsyncMock(side_effect=lambda *a, **k: self.publication)
        self.request = _Request(visitor or [])
        self.source = VisitorHeaderSource(self.request)  # type: ignore[arg-type]
        self.hits: list[dict[str, Any]] = []
        self.search = MagicMock()
        self.search.return_value.hybrid_search = AsyncMock(side_effect=self._search)
        self.degraded = False
        self.log_usage = AsyncMock()
        self.key: Any = None
        self.bound_bucket = AsyncMock()
        self.anon_bucket = AsyncMock(return_value=True)
        self.capacity = AsyncMock()
        self.slot = AsyncMock()
        self.charge = AsyncMock()
        self.now = datetime(2026, 11, 6, 12, 0, 0)

    async def _search(self, **kwargs: Any) -> list[dict[str, Any]]:
        if self.degraded:
            kwargs["degradation"]["degraded"] = True
            kwargs["degradation"]["reason"] = "embedding_unavailable"
        return list(self.hits)

    def bound(self) -> _Rig:
        self.key = SimpleNamespace(id=7, user_id=OWNER_ID, bound_context_id=self.context.id)
        return self

    async def run(self, *, limit: int = 5):
        with (
            patch.object(route, "check_pre_auth_rate_limit", AsyncMock()),
            patch.object(route, "_resolve_public_attribution", AsyncMock(return_value=self.key)),
            patch.object(route, "check_bound_key_rate_limit", self.bound_bucket),
            patch.object(route, "check_public_search_rate_limit", self.anon_bucket),
            patch.object(route, "_charge_public_usage_budget", self.charge),
            patch.object(route, "ensure_not_capacity_locked", self.capacity),
            patch.object(route, "ensure_slot_active", self.slot),
            patch.object(route, "SearchService", self.search),
            patch.object(route, "log_usage", self.log_usage),
            patch.object(route, "utcnow", lambda: self.now),
            patch.object(public_search_usage, "load_context_publication", self.lookup),
        ):
            return await public_search(
                context_id=self.context.id,
                request=PublicSearchRequest(query="q", limit=limit),
                api_key=TOKEN_MARKER if self.key is not None else None,
                db=self.db,  # type: ignore[arg-type]
                visitor=self.source,
            )

    @property
    def header_reads(self) -> int:
        return self.request.headers.reads


def _built(event: UsageEventInput):
    return build_usage_event_row(
        event, key=get_settings().audit_hmac_key, session_details_enabled=False
    )


def _event(logs: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [entry for entry in logs if entry["event"] == name]
    assert len(matches) == 1, f"expected one {name!r}, got {logs!r}"
    return matches[0]


# ---------------------------------------------------------------------------
# When recording runs at all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rest", "telemetry", "writer"),
    [(False, True, True), (True, False, True), (True, True, False), (False, False, False)],
)
async def test_nothing_is_recorded_unless_both_switches_and_a_writer(
    monkeypatch, rest: bool, telemetry: bool, writer: bool
) -> None:
    spy = _Writer()
    monkeypatch.setattr(get_settings(), "public_search_telemetry_enabled", rest)
    monkeypatch.setattr(get_settings(), "mcp_public_telemetry_enabled", telemetry)
    monkeypatch.setattr(writer_module, "_writer", spy if writer else None)
    rig = _Rig(visitor=["v-1"]).bound()
    with structlog.testing.capture_logs() as logs:
        response = await rig.run()
    assert response.status == "success"
    rig.lookup.assert_not_awaited()
    assert spy.offered == []
    assert rig.header_reads == 0
    line = _event(logs, "public_search_completed")
    assert line["recorded"] is False
    assert line["visitor_ignored"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["answered", "bucket", "budget_exhausted", "budget_unavailable", "plan", "capacity", "slot"],
)
async def test_an_anonymous_search_is_never_recorded(switches: _Writer, path: str) -> None:
    rig = _Rig(visitor=[VISITOR_MARKER], plan="free" if path == "plan" else "pro")
    expected: type[BaseException] | None = None
    if path == "bucket":
        rig.anon_bucket.side_effect = RateLimitError(
            "over", retry_after=60, reason="anonymous_limit"
        )
        expected = RateLimitError
    elif path == "budget_exhausted":
        rig.charge.side_effect = PublicUsageBudgetExhaustedError(retry_after=60)
        expected = PublicUsageBudgetExhaustedError
    elif path == "budget_unavailable":
        rig.charge.side_effect = PublicUsageBudgetUnavailableError()
        expected = PublicUsageBudgetUnavailableError
    elif path == "plan":
        expected = QuotaExceededError
    elif path == "capacity":
        rig.capacity.side_effect = CapacityLockedError.for_outsider()
        expected = CapacityLockedError
    elif path == "slot":
        rig.slot.side_effect = WorkspaceSlotSuspendedError.for_outsider()
        expected = WorkspaceSlotSuspendedError
    with structlog.testing.capture_logs() as logs:
        if expected is None:
            await rig.run()
        else:
            with pytest.raises(expected):
                await rig.run()
    rig.lookup.assert_not_awaited()
    assert switches.offered == []
    assert rig.header_reads == 0
    assert rig.db.commits == 0
    if path == "answered":
        line = _event(logs, "public_search_completed")
        assert "recorded" not in line and "visitor_ignored" not in line
        rig.charge.assert_awaited_once()


# ---------------------------------------------------------------------------
# A recorded bound-key search: one event, whatever the exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_answered_bound_search_is_one_rest_event(switches: _Writer) -> None:
    rig = _Rig().bound()
    note, doc, hidden = str(uuid4()), str(uuid4()), str(uuid4())
    rig.hits = [_hit(note), _hit(doc), _hit(hidden)]
    rig.db.rows = [_row(note, None, served=False), _row(doc, "doc-7"), _row(hidden, "doc-9", False)]
    with structlog.testing.capture_logs() as logs:
        response = await rig.run()
    assert [r.memory_id for r in response.results] == [note, doc, hidden]
    (event,) = switches.offered
    p = rig.publication
    assert p is not None
    assert (event.route, event.caller, event.era, event.tool) == (
        "rest",
        "bound_key",
        "stateless",
        "recall",
    )
    assert (event.endpoint_id, event.resource_pk, event.resource_id) == (
        p.id,
        p.resource_pk,
        p.resource_id,
    )
    assert (event.workspace_id, event.context_id) == (rig.context.workspace_id, rig.context.id)
    assert event.outcome == "success" and event.error_code is None
    assert event.result_count == 3
    # One entry per result in response order; "" where the endpoint serves none.
    assert event.doc_ids == ("", "doc-7", "")
    assert event.started_at == rig.now
    assert event.duration_ms >= 0
    assert event.protocol_version is None and event.session_id is None
    assert event.client_name is None
    rig.lookup.assert_awaited_once()
    assert rig.lookup.await_args.kwargs == {
        "context_id": rig.context.id,
        "workspace_id": rig.context.workspace_id,
    }
    assert _event(logs, "public_search_completed")["recorded"] is True
    rig.charge.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_degraded_bound_search_is_degraded(switches: _Writer) -> None:
    rig = _Rig().bound()
    rig.degraded = True
    await rig.run()
    assert [e.outcome for e in switches.offered] == ["degraded"]


@pytest.mark.asyncio
async def test_the_key_bucket_refusal_is_one_rate_limited_event(switches: _Writer) -> None:
    rig = _Rig(visitor=["v-1"]).bound()
    rig.bound_bucket.side_effect = RateLimitError("over", retry_after=60, reason="key_limit")
    with pytest.raises(RateLimitError) as exc:
        await rig.run()
    assert exc.value.details["reason"] == "key_limit"
    (event,) = switches.offered
    assert (event.outcome, event.error_code) == ("rate_limited", "rate_limit_exceeded")
    assert event.result_count is None and event.doc_ids == ()
    assert _built(event).visitor is None


@pytest.mark.asyncio
async def test_a_cancellation_in_the_bucket_is_one_cancelled_event(switches: _Writer) -> None:
    rig = _Rig().bound()
    rig.bound_bucket.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await rig.run()
    (event,) = switches.offered
    assert (event.outcome, event.error_code) == ("cancelled", None)


@pytest.mark.asyncio
async def test_limit_zero_starts_no_record(switches: _Writer) -> None:
    rig = _Rig(plan="free", visitor=["v-1"]).bound()
    rig.bound_bucket.side_effect = RateLimitError(
        "not provisioned", retry_after=60, reason="key_not_provisioned"
    )
    with pytest.raises(RateLimitError) as exc:
        await rig.run()
    assert exc.value.details["reason"] == "key_not_provisioned"
    rig.lookup.assert_not_awaited()
    assert rig.header_reads == 0
    assert switches.offered == []
    assert rig.bound_bucket.await_args.args == (7, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["capacity", "slot"])
async def test_a_serving_gate_refusal_leaves_no_event(switches: _Writer, gate: str) -> None:
    rig = _Rig().bound()
    if gate == "capacity":
        rig.capacity.side_effect = CapacityLockedError.for_outsider()
        expected: type[Exception] = CapacityLockedError
    else:
        rig.slot.side_effect = WorkspaceSlotSuspendedError.for_outsider()
        expected = WorkspaceSlotSuspendedError
    with pytest.raises(expected):
        await rig.run()
    assert switches.offered == []
    rig.lookup.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_search_that_cannot_run_is_an_error_event_not_a_skip(switches: _Writer) -> None:
    rig = _Rig().bound()
    rig.search.return_value.hybrid_search = AsyncMock(
        side_effect=EmbeddingSpendCapExceeded(
            "cap", period="daily", cap_usd=1.0, current_usd=2.0, workspace_id=None
        )
    )
    with pytest.raises(TemporarilyUnavailableError):
        await rig.run()
    (event,) = switches.offered
    assert (event.outcome, event.error_code) == ("error", "temporarily_unavailable")


@pytest.mark.asyncio
async def test_a_failed_search_is_an_internal_error_event(switches: _Writer) -> None:
    rig = _Rig().bound()
    rig.search.return_value.hybrid_search = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(HTTPException) as exc:
        await rig.run()
    assert exc.value.status_code == 500
    (event,) = switches.offered
    assert (event.outcome, event.error_code) == ("error", "internal_error")


@pytest.mark.asyncio
async def test_a_cancelled_search_is_one_cancelled_event(switches: _Writer) -> None:
    rig = _Rig().bound()
    rig.search.return_value.hybrid_search = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await rig.run()
    assert [(e.outcome, e.error_code) for e in switches.offered] == [("cancelled", None)]


@pytest.mark.asyncio
async def test_a_failure_after_the_answer_was_built_wins(switches: _Writer) -> None:
    rig = _Rig(visitor=["v-1"]).bound()
    rig.log_usage.side_effect = RuntimeError("usage log down")
    with pytest.raises(RuntimeError):
        await rig.run()
    (event,) = switches.offered
    assert (event.outcome, event.error_code) == ("error", "internal_error")
    assert event.result_count is None and event.doc_ids == ()
    assert _built(event).visitor is None


@pytest.mark.asyncio
async def test_no_serving_publication_records_nothing(switches: _Writer) -> None:
    rig = _Rig(visitor=["v-1"]).bound()
    rig.publication = None
    with structlog.testing.capture_logs() as logs:
        response = await rig.run()
    assert response.status == "success"
    assert switches.offered == []
    assert rig.header_reads == 0
    assert _event(logs, "public_search_completed")["recorded"] is False


@pytest.mark.asyncio
async def test_a_failed_lookup_records_nothing_and_serves(switches: _Writer) -> None:
    rig = _Rig().bound()
    rig.lookup.side_effect = RuntimeError("relation does not exist: secret-detail")
    with structlog.testing.capture_logs() as logs:
        response = await rig.run()
    assert response.status == "success"
    assert switches.offered == []
    warning = _event(logs, "public_search_publication_unread")
    assert set(warning) == {"event", "log_level", "error_type"}
    assert warning["error_type"] == "RuntimeError"
    assert "secret-detail" not in json.dumps(logs, default=str)


@pytest.mark.asyncio
async def test_a_failed_filter_read_records_no_documents(switches: _Writer) -> None:
    rig = _Rig().bound()
    a, b = str(uuid4()), str(uuid4())
    rig.hits = [_hit(a), _hit(a), _hit(b)]
    rig.db.execute_error = RuntimeError("read failed")
    with structlog.testing.capture_logs() as logs:
        response = await rig.run()
    assert [r.memory_id for r in response.results] == [a, b]
    _event(logs, "public_search_filter_unread")
    (event,) = switches.offered
    assert event.outcome == "success"
    assert event.doc_ids == ()
    assert event.result_count == 2


@pytest.mark.asyncio
async def test_an_offer_that_raises_changes_nothing(switches: _Writer) -> None:
    rig = _Rig().bound()
    switches.error = RuntimeError("queue gone")
    with structlog.testing.capture_logs() as logs:
        response = await rig.run()
    assert response.status == "success"
    assert _event(logs, "public_search_usage_failed")["error_type"] == "RuntimeError"


# ---------------------------------------------------------------------------
# Visitors
# ---------------------------------------------------------------------------


async def _visitor_case(values: list[str]) -> tuple[_Rig, UsageEventInput, dict[str, Any]]:
    rig = _Rig(visitor=values).bound()
    with structlog.testing.capture_logs() as logs:
        await rig.run()
    (event,) = writer_module._writer.offered  # type: ignore[union-attr]
    return rig, event, _event(logs, "public_search_completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["v", "A-z_09", "x" * 64])
async def test_one_well_formed_value_is_the_daily_key(switches: _Writer, value: str) -> None:
    rig, event, line = await _visitor_case([value])
    assert rig.publication is not None
    expected = visitor_key(rig.publication.id, value, rig.now.date(), get_settings().audit_hmac_key)
    assert event.visitor_key == expected
    assert line["visitor_ignored"] is None
    assert rig.header_reads == 1
    built = _built(event)
    assert built.visitor is not None and built.visitor.visitor_key == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["x" * 65, "", "a.b", "a@b", "a,b", "a b", "ä", "名前"])
async def test_a_malformed_value_is_ignored_as_format(switches: _Writer, value: str) -> None:
    _, event, line = await _visitor_case([value])
    assert event.visitor_key is None
    assert line["visitor_ignored"] == "format"
    assert _built(event).visitor is None


@pytest.mark.asyncio
async def test_two_headers_are_ignored_as_repeated(switches: _Writer) -> None:
    _, event, line = await _visitor_case(["v-1", "v-2"])
    assert event.visitor_key is None
    assert line["visitor_ignored"] == "repeated"


@pytest.mark.asyncio
async def test_no_header_is_no_key_and_no_reason(switches: _Writer) -> None:
    _, event, line = await _visitor_case([])
    assert event.visitor_key is None
    assert line["visitor_ignored"] is None


@pytest.mark.asyncio
async def test_the_key_is_of_the_routes_start_day(switches: _Writer, monkeypatch) -> None:
    """The route starts at 23:59:59.900 of day D; every later clock reads D+1."""
    day_end = datetime(2026, 11, 6, 23, 59, 59, 900_000)
    next_day = datetime(2026, 11, 7, 0, 0, 1)
    monkeypatch.setattr(writer_module, "utcnow", lambda: next_day)
    monkeypatch.setattr("services.public_mcp_usage_events.utcnow", lambda: next_day)
    rig = _Rig(visitor=["v-late"]).bound()
    rig.now = day_end
    await rig.run()
    (event,) = switches.offered
    assert rig.publication is not None
    key = get_settings().audit_hmac_key
    assert event.visitor_key == visitor_key(rig.publication.id, "v-late", day_end.date(), key)
    assert event.visitor_key != visitor_key(rig.publication.id, "v-late", next_day.date(), key)
    built = _built(event)
    assert built.visitor is not None and built.visitor.day == day_end.date()


def test_the_source_reads_only_the_visitor_header_and_only_when_asked() -> None:
    request = _Request(["v-1"])
    source = visitor_header_source(request)  # type: ignore[arg-type]
    assert request.headers.reads == 0
    assert source.values() == ["v-1"]
    assert request.headers.reads == 1
    assert repr(source) == "VisitorHeaderSource()"


@pytest.mark.asyncio
async def test_a_direct_call_without_the_dependency_reads_no_header(switches: _Writer) -> None:
    rig = _Rig().bound()
    with (
        patch.object(route, "check_pre_auth_rate_limit", AsyncMock()),
        patch.object(route, "_resolve_public_attribution", AsyncMock(return_value=rig.key)),
        patch.object(route, "check_bound_key_rate_limit", AsyncMock()),
        patch.object(route, "ensure_not_capacity_locked", AsyncMock()),
        patch.object(route, "ensure_slot_active", AsyncMock()),
        patch.object(route, "SearchService", rig.search),
        patch.object(route, "log_usage", AsyncMock()),
        patch.object(public_search_usage, "load_context_publication", rig.lookup),
    ):
        await public_search(
            context_id=rig.context.id,
            request=PublicSearchRequest(query="q"),
            api_key="kagura_x",
            db=rig.db,  # type: ignore[arg-type]
        )
    (event,) = switches.offered
    assert event.visitor_key is None


# ---------------------------------------------------------------------------
# Neither the key nor the visitor value goes anywhere
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_neither_marker_leaves_the_request(switches: _Writer) -> None:
    rig = _Rig(visitor=[VISITOR_MARKER]).bound()
    doc = str(uuid4())
    rig.hits = [_hit(doc)]
    rig.db.rows = [_row(doc, "doc-1")]
    with structlog.testing.capture_logs() as logs:
        response = await rig.run()
    (event,) = switches.offered
    record = public_search_usage.BoundSearchRecord(
        publication=rig.publication,  # type: ignore[arg-type]
        started_at=rig.now,
        clock_start=0.0,
        visitor=VisitorHeaderSource(_Request([VISITOR_MARKER])),  # type: ignore[arg-type]
    )
    record.resolve_visitor()
    texts = [
        json.dumps(logs, default=str),
        repr(event),
        repr(rig.source),
        repr(record),
        response.model_dump_json(),
        json.dumps(_built(event).row, default=str),
    ]
    for text in texts:
        for marker in ("TOKENMARKER3035", VISITOR_MARKER, "MARKER-3035"):
            assert marker not in text
    assert event.visitor_key is not None


@pytest.mark.asyncio
async def test_a_refused_bound_search_offers_once_and_nothing_twice(switches: _Writer) -> None:
    rig = _Rig().bound()
    rig.bound_bucket.side_effect = RateLimitError("over", retry_after=60, reason="key_limit")
    with pytest.raises(RateLimitError):
        await rig.run()
    assert len(switches.offered) == 1


def test_the_null_record_does_nothing() -> None:
    record = public_search_usage.NULL_RECORD
    assert record.live is False
    assert record.publication is None
    with record.gate():
        pass
    record.resolve_visitor()
    record.served(degraded=False, result_count=1, doc_ids=())
    record.failed(RuntimeError())
    record.emit()
