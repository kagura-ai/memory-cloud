"""Characterization tests for the shared Resource batch-ingest service.

Issue #1255: REST ``ingest_batch`` and MCP ``handle_ingest_events`` become
thin adapters over ``services.resource_ingest_service``. These tests pin the
domain semantics both surfaces previously implemented independently, plus the
per-surface wire strings the adapters must keep byte-compatible.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from db.constraint_names import (
    RESOURCE_EVENTS_IDEMPOTENCY_UNIQUE,
    RESOURCE_EVENTS_UPSERT_UNIQUE,
)
from services import resource_ingest_service as svc
from services.resource_ingest_service import (
    IngestEventInput,
    IngestItemError,
    validate_events,
)

# ============================================================================
# validate_events — domain validation shared by both surfaces
# ============================================================================


class TestValidateEvents:
    def test_valid_upsert_passes(self):
        valid, errors = validate_events(
            [{"op": "upsert", "doc_id": "d1", "version": 1, "payload": {"x": 1}}]
        )
        assert errors == []
        assert len(valid) == 1
        ev = valid[0]
        assert ev.index == 0
        assert ev.op == "upsert"
        assert ev.doc_id == "d1"
        assert ev.version == 1
        assert ev.payload == {"x": 1}
        assert ev.importance == svc.DEFAULT_IMPORTANCE

    def test_valid_delete_passes_without_version(self):
        valid, errors = validate_events([{"op": "delete", "doc_id": "d1"}])
        assert errors == []
        assert valid[0].version is None
        assert valid[0].payload is None

    def test_valid_delete_with_version(self):
        valid, errors = validate_events([{"op": "delete", "doc_id": "d1", "version": 5}])
        assert errors == []
        assert valid[0].version == 5

    @pytest.mark.parametrize(
        ("event", "kind"),
        [
            ("not a dict", svc.KIND_NOT_AN_OBJECT),
            ({"op": "bogus", "doc_id": "d1"}, svc.KIND_INVALID_OP),
            ({"op": "upsert"}, svc.KIND_MISSING_DOC_ID),
            ({"op": "upsert", "doc_id": "d1"}, svc.KIND_PAYLOAD_REQUIRED),
            (
                {"op": "upsert", "doc_id": "d1", "payload": {"x": 1}, "version": "nan"},
                svc.KIND_VERSION_NOT_INT,
            ),
            (
                {"op": "upsert", "doc_id": "d1", "payload": {"x": 1}},
                svc.KIND_VERSION_TOO_SMALL_UPSERT,
            ),
            (
                {"op": "upsert", "doc_id": "d1", "payload": {"x": 1}, "version": 0},
                svc.KIND_VERSION_TOO_SMALL_UPSERT,
            ),
            (
                {
                    "op": "upsert",
                    "doc_id": "d1",
                    "payload": {"x": 1},
                    "version": 1,
                    "importance": "high",
                },
                svc.KIND_IMPORTANCE_NOT_NUMBER,
            ),
            (
                {
                    "op": "upsert",
                    "doc_id": "d1",
                    "payload": {"x": 1},
                    "version": 1,
                    "importance": 1.5,
                },
                svc.KIND_IMPORTANCE_OUT_OF_RANGE,
            ),
            (
                {"op": "delete", "doc_id": "d1", "payload": {"x": 1}},
                svc.KIND_PAYLOAD_NOT_NULL_DELETE,
            ),
            ({"op": "delete", "doc_id": "d1", "version": 0}, svc.KIND_VERSION_TOO_SMALL),
            ({"op": "delete", "doc_id": "d1", "version": "nan"}, svc.KIND_VERSION_NOT_INT),
        ],
    )
    def test_invalid_events_classified(self, event, kind):
        valid, errors = validate_events([event])
        assert valid == []
        assert len(errors) == 1
        assert errors[0].kind == kind
        assert errors[0].index == 0

    def test_payload_size_is_measured_in_bytes(self):
        # Multibyte payload: character count is under the cap, byte count over.
        # The service uses bytes — the semantic MAX_PAYLOAD_SIZE_BYTES names.
        big = {"t": "あ" * (svc.MAX_PAYLOAD_SIZE_BYTES // 3)}
        valid, errors = validate_events(
            [{"op": "upsert", "doc_id": "d1", "version": 1, "payload": big}]
        )
        assert valid == []
        assert errors[0].kind == svc.KIND_PAYLOAD_TOO_LARGE
        assert errors[0].detail["payload_size"] > svc.MAX_PAYLOAD_SIZE_BYTES

    def test_explicit_null_importance_uses_default(self):
        # Unified semantic: explicit null == absent == default (REST behavior;
        # the MCP path previously rejected an explicit null).
        valid, errors = validate_events(
            [
                {
                    "op": "upsert",
                    "doc_id": "d1",
                    "version": 1,
                    "payload": {"x": 1},
                    "importance": None,
                }
            ]
        )
        assert errors == []
        assert valid[0].importance == svc.DEFAULT_IMPORTANCE

    def test_oversized_delete_payload_reports_size_before_null_rule(self):
        # Precedence pin: the payload-size check runs before the
        # delete-payload-must-be-null rule (both surfaces' historic order).
        big = {"t": "x" * (svc.MAX_PAYLOAD_SIZE_BYTES + 1)}
        valid, errors = validate_events([{"op": "delete", "doc_id": "d1", "payload": big}])
        assert errors[0].kind == svc.KIND_PAYLOAD_TOO_LARGE

    def test_indices_preserved_across_mixed_batch(self):
        valid, errors = validate_events(
            [
                {"op": "bogus", "doc_id": "d0"},
                {"op": "upsert", "doc_id": "d1", "version": 1, "payload": {"x": 1}},
                {"op": "upsert", "doc_id": "d2"},
            ]
        )
        assert [e.index for e in errors] == [0, 2]
        assert [v.index for v in valid] == [1]


# ============================================================================
# persist_events — SAVEPOINT processing + constraint classification
# ============================================================================


def _make_input(index: int = 0, **overrides) -> IngestEventInput:
    base = {
        "index": index,
        "op": "upsert",
        "doc_id": f"d{index}",
        "version": 1,
        "payload": {"x": 1},
        "idempotency_key": None,
        "event_metadata": {},
        "importance": 0.6,
    }
    base.update(overrides)
    return IngestEventInput(**base)


def _mock_db(connector_id=None):
    db = AsyncMock()
    connector_result = MagicMock()
    connector_result.scalar_one_or_none.return_value = connector_id
    db.execute.side_effect = [connector_result]
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    nested = AsyncMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=None)
    db.begin_nested = MagicMock(return_value=nested)
    return db


def _integrity_error_for(constraint: str) -> IntegrityError:
    # integrity_error_constraint_name reads error.orig.constraint_name
    # (asyncpg shape) first — attach the name directly to the orig exception.
    orig = Exception(f'violates constraint "{constraint}"')
    orig.constraint_name = constraint  # type: ignore[attr-defined]
    return IntegrityError("stmt", {}, orig)


class TestPersistEvents:
    @pytest.mark.asyncio
    async def test_created_event_id_collected(self):
        db = _mock_db()

        def _add(obj):
            obj.id = 42

        db.add = MagicMock(side_effect=_add)
        result = await svc.persist_events(
            db, resource_id="res1", resource_pk=uuid4(), events=[_make_input()]
        )
        assert result.created_ids == [42]
        assert result.errors == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("constraint", "kind"),
        [
            (RESOURCE_EVENTS_UPSERT_UNIQUE, svc.KIND_DUPLICATE_VERSION),
            (RESOURCE_EVENTS_IDEMPOTENCY_UNIQUE, svc.KIND_DUPLICATE_IDEMPOTENCY),
            ("some_other_constraint", svc.KIND_CONSTRAINT_VIOLATION),
        ],
    )
    async def test_integrity_errors_classified_by_constraint(self, constraint, kind):
        db = _mock_db()
        db.flush = AsyncMock(side_effect=_integrity_error_for(constraint))
        result = await svc.persist_events(
            db, resource_id="res1", resource_pk=uuid4(), events=[_make_input()]
        )
        assert result.created_ids == []
        assert len(result.errors) == 1
        assert result.errors[0].kind == kind

    @pytest.mark.asyncio
    async def test_unexpected_error_recorded_and_siblings_continue(self):
        # Partial success: a non-IntegrityError on one event must not abort
        # the sibling events (previously the MCP path failed the whole call).
        db = _mock_db()
        ids = iter([1, 2])

        def _add(obj):
            obj.id = next(ids)

        db.add = MagicMock(side_effect=_add)
        db.flush = AsyncMock(side_effect=[RuntimeError("boom"), None])
        result = await svc.persist_events(
            db,
            resource_id="res1",
            resource_pk=uuid4(),
            events=[_make_input(0), _make_input(1)],
        )
        assert len(result.errors) == 1
        assert result.errors[0].kind == svc.KIND_UNEXPECTED
        assert result.errors[0].index == 0
        assert len(result.created_ids) == 1

    @pytest.mark.asyncio
    async def test_invalid_idempotency_key_recorded_per_item(self):
        db = _mock_db(connector_id=uuid4())
        with patch(
            "services.connector_provisioning.validate_connector_idempotency_key"
        ) as mock_validate:
            from utils.exceptions import ValidationError

            mock_validate.side_effect = ValidationError("bad prefix")
            result = await svc.persist_events(
                db,
                resource_id="res1",
                resource_pk=uuid4(),
                events=[_make_input(idempotency_key="wrong")],
            )
        assert result.created_ids == []
        assert result.errors[0].kind == svc.KIND_IDEMPOTENCY_INVALID
        assert result.errors[0].detail["message"] == "bad prefix"


# ============================================================================
# finalize_batch — the post-commit indexer boundary
# ============================================================================


class TestFinalizeBatch:
    @pytest.mark.asyncio
    async def test_commits_then_schedules_then_commits(self):
        db = AsyncMock()
        calls: list[str] = []
        db.commit = AsyncMock(side_effect=lambda: calls.append("commit"))

        async def _schedule(db_, ws, rid):
            calls.append("schedule")

        with patch("api.routes.resource_ingest._schedule_indexer_for_resource", new=_schedule):
            await svc.finalize_batch(db, workspace_id=uuid4(), resource_id="res1", created_ids=[1])
        assert calls == ["commit", "schedule", "commit"]

    @pytest.mark.asyncio
    async def test_no_indexer_when_nothing_created(self):
        db = AsyncMock()
        db.commit = AsyncMock()
        with patch(
            "api.routes.resource_ingest._schedule_indexer_for_resource",
            new=AsyncMock(),
        ) as mock_schedule:
            await svc.finalize_batch(db, workspace_id=uuid4(), resource_id="res1", created_ids=[])
        mock_schedule.assert_not_awaited()
        db.commit.assert_awaited_once()


# ============================================================================
# Adapter wire-format parity — the per-surface strings are preserved
# ============================================================================


class TestAdapterErrorFormatting:
    def test_rest_strings_are_byte_compatible(self):
        from api.routes.resource_ingest import _format_batch_item_error as fmt

        cases = {
            svc.KIND_PAYLOAD_TOO_LARGE: (
                {"payload_size": 123456, "max": svc.MAX_PAYLOAD_SIZE_BYTES},
                "Payload too large: 123456 bytes",
            ),
            svc.KIND_IDEMPOTENCY_INVALID: ({"message": "bad prefix"}, "bad prefix"),
            svc.KIND_DUPLICATE_VERSION: ({"version": 3}, "Duplicate version 3"),
            svc.KIND_DUPLICATE_IDEMPOTENCY: ({}, "Duplicate idempotency key"),
            svc.KIND_CONSTRAINT_VIOLATION: (
                {"constraint": "x"},
                "Database constraint violation",
            ),
            svc.KIND_UNEXPECTED: ({"message": "boom"}, "boom"),
        }
        for kind, (detail, expected) in cases.items():
            out = fmt(IngestItemError(index=1, kind=kind, doc_id="d1", detail=detail))
            assert out == {"index": 1, "doc_id": "d1", "error": expected}, kind

    def test_mcp_strings_are_byte_compatible(self):
        from mcp_server.tools.resource import _format_batch_item_error as fmt

        cases = {
            svc.KIND_NOT_AN_OBJECT: ({}, "event must be an object", False),
            svc.KIND_INVALID_OP: ({"op": "bogus"}, "Invalid op: bogus", False),
            svc.KIND_MISSING_DOC_ID: ({}, "Missing doc_id", False),
            svc.KIND_PAYLOAD_REQUIRED: ({}, "payload required for upsert", False),
            svc.KIND_VERSION_NOT_INT: ({}, "version must be an integer", False),
            svc.KIND_VERSION_TOO_SMALL_UPSERT: (
                {},
                "version >= 1 required for upsert",
                False,
            ),
            svc.KIND_PAYLOAD_TOO_LARGE: (
                {"payload_size": 123456, "max": svc.MAX_PAYLOAD_SIZE_BYTES},
                f"Payload too large: 123456 bytes (max {svc.MAX_PAYLOAD_SIZE_BYTES})",
                False,
            ),
            svc.KIND_IMPORTANCE_NOT_NUMBER: ({}, "importance must be a number", False),
            svc.KIND_IMPORTANCE_OUT_OF_RANGE: (
                {},
                "importance must be between 0.0 and 1.0",
                False,
            ),
            svc.KIND_PAYLOAD_NOT_NULL_DELETE: (
                {},
                "payload must be null for delete",
                False,
            ),
            svc.KIND_VERSION_TOO_SMALL: ({}, "version must be >= 1", False),
            svc.KIND_IDEMPOTENCY_INVALID: ({"message": "bad prefix"}, "bad prefix", True),
            svc.KIND_DUPLICATE_VERSION: (
                {"version": 3},
                "Duplicate version for doc_id=d1",
                False,
            ),
            svc.KIND_DUPLICATE_IDEMPOTENCY: ({}, "Duplicate idempotency_key", False),
            svc.KIND_CONSTRAINT_VIOLATION: (
                {"constraint": "x"},
                "Unable to ingest event due to a constraint violation",
                False,
            ),
        }
        for kind, (detail, expected, has_doc_id) in cases.items():
            out = fmt(IngestItemError(index=1, kind=kind, doc_id="d1", detail=detail))
            assert out["index"] == 1, kind
            assert out["error"] == expected, kind
            assert ("doc_id" in out) is has_doc_id, kind
