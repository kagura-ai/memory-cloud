"""Tests for TZAwareBaseModel and per-schema datetime serialization.

Issue #489: ensure every response schema with a datetime field produces
JS-parseable UTC-marked ISO 8601 strings (`Z` suffix for naive/UTC inputs,
`+HH:MM` for non-UTC inputs), and avoids the double-Z regression class.

Coverage:
- TZAwareBaseModel: None / naive / aware-UTC / aware-non-UTC inputs
- All response schemas previously bearing per-field serializers (must NOT
  produce `+00:00Z` double suffix on tz-aware UTC inputs)
- Schemas with bare `datetime` fields and no per-field serializer (the
  primary bug surface — must NOT serialize as naive ISO without Z)
"""

import json
from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from api.routes.api_keys import APIKeyResponse
from api.routes.bm25_drift import Bm25DriftSummary
from api.routes.contexts import ContextResponse
from api.routes.sleep_reports import SleepReportSummary
from api.routes.workspaces import ContextStatsItem, UserActivityItem
from models.api_base import TZAwareBaseModel
from models.schemas import (
    MemoryResponse,
    ReferenceResponse,
    UserProfileResponse,
    UserWithAdminFlag,
)

# ============================================================================
# TZAwareBaseModel direct tests
# ============================================================================


class _SchemaWithMandatoryAndNullable(TZAwareBaseModel):
    name: str
    created_at: datetime
    updated_at: datetime | None
    n: int


class TestTZAwareBaseModel:
    def test_naive_datetime_emits_z_suffix(self):
        m = _SchemaWithMandatoryAndNullable(
            name="x",
            created_at=datetime(2026, 4, 28, 17, 50, 22),
            updated_at=None,
            n=1,
        )
        body = json.loads(m.model_dump_json())
        assert body["created_at"] == "2026-04-28T17:50:22Z"
        assert body["updated_at"] is None
        assert body["name"] == "x"
        assert body["n"] == 1

    def test_tz_aware_utc_does_not_double_suffix(self):
        m = _SchemaWithMandatoryAndNullable(
            name="x",
            created_at=datetime(2026, 4, 28, 17, 50, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 28, 17, 50, 22, tzinfo=UTC),
            n=1,
        )
        body = json.loads(m.model_dump_json())
        # Crucial regression guard: must NOT be "...:22+00:00Z" or "...:22ZZ"
        assert body["created_at"] == "2026-04-28T17:50:22Z"
        assert body["updated_at"] == "2026-04-28T17:50:22Z"

    def test_non_utc_aware_preserves_offset(self):
        # JST input — TZAwareBaseModel's role is to mark UTC as Z, not to
        # convert. Non-UTC offsets are kept verbatim so callers retain the
        # original wall-clock context.
        m = _SchemaWithMandatoryAndNullable(
            name="x",
            created_at=datetime(2026, 4, 28, 17, 50, 22, tzinfo=ZoneInfo("Asia/Tokyo")),
            updated_at=None,
            n=1,
        )
        body = json.loads(m.model_dump_json())
        assert body["created_at"] == "2026-04-28T17:50:22+09:00"

    def test_nullable_datetime_none_serializes_to_null(self):
        m = _SchemaWithMandatoryAndNullable(
            name="x",
            created_at=datetime(2026, 4, 28, 17, 50, 22),
            updated_at=None,
            n=1,
        )
        body = json.loads(m.model_dump_json())
        assert body["updated_at"] is None

    def test_non_datetime_fields_pass_through_correctly(self):
        # Wildcard serializer with mode='wrap' must delegate non-datetime
        # values to the default JSON serializer.
        m = _SchemaWithMandatoryAndNullable(
            name="hello",
            created_at=datetime(2026, 4, 28),
            updated_at=None,
            n=42,
        )
        body = json.loads(m.model_dump_json())
        assert body["name"] == "hello"
        assert body["n"] == 42

    def test_python_dict_dump_does_not_apply_z_suffix(self):
        # when_used='json' means model_dump() (Python dict) keeps the raw
        # datetime, not the string. This preserves type info for in-process
        # consumers (e.g. internal service-layer pass-through).
        m = _SchemaWithMandatoryAndNullable(
            name="x",
            created_at=datetime(2026, 4, 28, 17, 50, 22),
            updated_at=None,
            n=1,
        )
        body = m.model_dump()
        assert isinstance(body["created_at"], datetime)


# ============================================================================
# Per-schema regression tests: schemas that previously had their own
# `field_serializer` MUST still emit Z (and never double-Z) after migrating
# to TZAwareBaseModel inheritance.
# ============================================================================


class TestMemoryResponseSerialization:
    def test_naive_created_at_gets_z(self):
        m = MemoryResponse(
            memory_id=uuid4(),
            summary="s",
            context_summary=None,
            type="note",
            importance=0.5,
            scope="persistent",
            created_at=datetime(2026, 4, 28, 17, 50, 22),
            client="t",
            tags=[],
            context=None,
        )
        body = json.loads(m.model_dump_json())
        assert body["created_at"] == "2026-04-28T17:50:22Z"

    def test_aware_utc_created_at_no_double_z(self):
        m = MemoryResponse(
            memory_id=uuid4(),
            summary="s",
            context_summary=None,
            type="note",
            importance=0.5,
            scope="persistent",
            created_at=datetime(2026, 4, 28, 17, 50, 22, tzinfo=UTC),
            client="t",
            tags=[],
            context=None,
        )
        body = json.loads(m.model_dump_json())
        assert body["created_at"] == "2026-04-28T17:50:22Z"
        assert "ZZ" not in body["created_at"]
        assert "+00:00Z" not in body["created_at"]


class TestReferenceResponseSerialization:
    def test_naive_created_and_updated_get_z(self):
        r = ReferenceResponse(
            memory_id=uuid4(),
            summary="s",
            context_summary=None,
            content="c",
            details=None,
            type="note",
            scope="persistent",
            importance=0.5,
            tags=[],
            context=None,
            created_at=datetime(2026, 4, 28, 17, 50, 22),
            updated_at=datetime(2026, 4, 28, 18, 0, 0),
            client="t",
        )
        body = json.loads(r.model_dump_json())
        assert body["created_at"] == "2026-04-28T17:50:22Z"
        assert body["updated_at"] == "2026-04-28T18:00:00Z"

    def test_aware_utc_no_double_z(self):
        r = ReferenceResponse(
            memory_id=uuid4(),
            summary="s",
            context_summary=None,
            content="c",
            details=None,
            type="note",
            scope="persistent",
            importance=0.5,
            tags=[],
            context=None,
            created_at=datetime(2026, 4, 28, 17, 50, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 28, 18, 0, 0, tzinfo=UTC),
            client="t",
        )
        body = json.loads(r.model_dump_json())
        assert body["created_at"] == "2026-04-28T17:50:22Z"
        assert body["updated_at"] == "2026-04-28T18:00:00Z"


class TestUserWithAdminFlagSerialization:
    def test_naive_created_at_gets_z_and_nullable_last_login_handled(self):
        u = UserWithAdminFlag(
            id=1,
            email="a@b.c",
            user_id="u1",
            name="Foo",
            picture=None,
            role="user",
            is_initial_admin=False,
            created_at=datetime(2026, 4, 28, 17, 50, 22),
            last_login_at=None,
            memory_count=0,
            is_active=True,
        )
        body = json.loads(u.model_dump_json())
        assert body["created_at"] == "2026-04-28T17:50:22Z"
        assert body["last_login_at"] is None

    def test_naive_last_login_at_also_gets_z(self):
        u = UserWithAdminFlag(
            id=1,
            email="a@b.c",
            user_id="u1",
            name="Foo",
            picture=None,
            role="user",
            is_initial_admin=False,
            created_at=datetime(2026, 4, 28, 17, 50, 22),
            last_login_at=datetime(2026, 4, 28, 18, 0, 0),
            memory_count=0,
            is_active=True,
        )
        body = json.loads(u.model_dump_json())
        assert body["last_login_at"] == "2026-04-28T18:00:00Z"


class TestSleepReportSummarySerialization:
    def test_naive_started_and_completed_get_z(self):
        s = SleepReportSummary(
            id=uuid4(),
            user_id="u",
            workspace_id=None,
            context_id=None,
            status="completed",
            started_at=datetime(2026, 4, 28, 17, 50, 22),
            completed_at=datetime(2026, 4, 28, 18, 0, 0),
            memories_processed=0,
            edges_created=0,
            memories_merged=0,
            memories_promoted=0,
            memories_flagged=0,
            llm_calls_made=0,
            llm_tokens_used=0,
        )
        body = json.loads(s.model_dump_json())
        assert body["started_at"] == "2026-04-28T17:50:22Z"
        assert body["completed_at"] == "2026-04-28T18:00:00Z"

    def test_completed_at_none_handled(self):
        s = SleepReportSummary(
            id=uuid4(),
            user_id="u",
            workspace_id=None,
            context_id=None,
            status="running",
            started_at=datetime(2026, 4, 28, 17, 50, 22),
            completed_at=None,
            memories_processed=0,
            edges_created=0,
            memories_merged=0,
            memories_promoted=0,
            memories_flagged=0,
            llm_calls_made=0,
            llm_tokens_used=0,
        )
        body = json.loads(s.model_dump_json())
        assert body["completed_at"] is None


# ============================================================================
# Schemas previously WITHOUT a per-field serializer — these are the primary
# bug surface from issue #489. Pre-fix they emitted naive ISO without Z.
# ============================================================================


class TestContextResponseSerialization:
    """Issue #489 acceptance criterion: /api/v1/contexts datetime fields with Z."""

    def test_all_three_datetime_fields_get_z(self):
        c = ContextResponse(
            id=uuid4(),
            name="ctx",
            display_name=None,
            description=None,
            summary=None,
            usage_guide=None,
            is_default=False,
            is_private=True,
            is_public=False,
            resource_id=None,
            is_locked=False,
            created_by=None,
            created_by_name=None,
            created_at=datetime(2026, 4, 28, 17, 50, 22),
            updated_at=datetime(2026, 4, 28, 18, 0, 0),
            use_rerank=None,
            reranker_provider=None,
            embedding_model=None,
            embedding_dimensions=None,
            member_count=None,
            memory_count=0,
            last_activity_at=datetime(2026, 4, 28, 19, 0, 0),
        )
        body = json.loads(c.model_dump_json())
        assert body["created_at"] == "2026-04-28T17:50:22Z"
        assert body["updated_at"] == "2026-04-28T18:00:00Z"
        assert body["last_activity_at"] == "2026-04-28T19:00:00Z"

    def test_nullable_datetime_fields_none_handled(self):
        c = ContextResponse(
            id=uuid4(),
            name="ctx",
            display_name=None,
            description=None,
            summary=None,
            usage_guide=None,
            is_default=False,
            is_private=True,
            is_public=False,
            resource_id=None,
            is_locked=False,
            created_by=None,
            created_by_name=None,
            created_at=datetime(2026, 4, 28, 17, 50, 22),
            updated_at=None,
            use_rerank=None,
            reranker_provider=None,
            embedding_model=None,
            embedding_dimensions=None,
            member_count=None,
            memory_count=0,
            last_activity_at=None,
        )
        body = json.loads(c.model_dump_json())
        assert body["updated_at"] is None
        assert body["last_activity_at"] is None


class TestAPIKeyResponseSerialization:
    def test_all_four_datetime_fields_handle_naive_and_none(self):
        k = APIKeyResponse(
            id=1,
            key_prefix="prefix0123456789",
            name="test",
            user_id="u",
            created_at=datetime(2026, 4, 28, 17, 50, 22),
            last_used_at=None,
            revoked_at=None,
            expires_at=None,
            status="active",
        )
        body = json.loads(k.model_dump_json())
        assert body["created_at"] == "2026-04-28T17:50:22Z"
        assert body["last_used_at"] is None
        assert body["revoked_at"] is None
        assert body["expires_at"] is None

    def test_aware_utc_no_double_z(self):
        k = APIKeyResponse(
            id=1,
            key_prefix="prefix0123456789",
            name="test",
            user_id="u",
            created_at=datetime(2026, 4, 28, 17, 50, 22, tzinfo=UTC),
            last_used_at=datetime(2026, 4, 28, 18, 0, 0, tzinfo=UTC),
            revoked_at=None,
            expires_at=None,
            status="active",
        )
        body = json.loads(k.model_dump_json())
        assert body["created_at"] == "2026-04-28T17:50:22Z"
        assert body["last_used_at"] == "2026-04-28T18:00:00Z"


class TestUserProfileResponseSerialization:
    def test_naive_created_and_nullable_last_login(self):
        u = UserProfileResponse(
            id=1,
            email="a@b.c",
            name=None,
            picture=None,
            timezone="Asia/Tokyo",
            locale="ja",
            role="user",
            current_workspace_id=None,
            created_at=datetime(2026, 4, 28, 17, 50, 22),
            last_login_at=None,
        )
        body = json.loads(u.model_dump_json())
        assert body["created_at"] == "2026-04-28T17:50:22Z"
        assert body["last_login_at"] is None


class TestContextStatsItemSerialization:
    """Workspace stats endpoint: GET /api/v1/workspaces/{id}/contexts/stats."""

    def test_naive_last_activity_gets_z(self):
        item = ContextStatsItem(
            context_id="ctx-1",
            context_name="ctx",
            memory_count=5,
            last_activity=datetime(2026, 4, 28, 17, 50, 22),
            member_count=2,
        )
        body = json.loads(item.model_dump_json())
        assert body["last_activity"] == "2026-04-28T17:50:22Z"

    def test_null_last_activity_handled(self):
        item = ContextStatsItem(
            context_id="ctx-1",
            context_name="ctx",
            memory_count=0,
            last_activity=None,
            member_count=1,
        )
        body = json.loads(item.model_dump_json())
        assert body["last_activity"] is None


class TestUserActivityItemSerialization:
    def test_naive_last_activity_gets_z(self):
        item = UserActivityItem(
            user_id="u",
            user_name=None,
            user_email=None,
            api_calls=10,
            last_activity=datetime(2026, 4, 28, 17, 50, 22),
        )
        body = json.loads(item.model_dump_json())
        assert body["last_activity"] == "2026-04-28T17:50:22Z"


class TestBm25DriftSummarySerialization:
    def test_naive_measured_at_gets_z(self):
        s = Bm25DriftSummary(
            id=1,
            context_id=uuid4(),
            measured_at=datetime(2026, 4, 28, 17, 50, 22),
            psi=0.1,
            psi_status="green",
            m_memory_points=100,
            r_resource_points=50,
            num_terms=20,
        )
        body = json.loads(s.model_dump_json())
        assert body["measured_at"] == "2026-04-28T17:50:22Z"
