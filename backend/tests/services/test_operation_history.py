"""Mutation evidence is allowlisted, attributed and transaction-local (#2739)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from config.plan_tiers import FeatureEnforcement, feature_enforcement, has_feature
from models.auth import Context
from models.operation_history import OperationHistory
from services.operation_history import decode_cursor, encode_cursor
from services.operation_history_writer import record_operation
from services.write_provenance import WriteProvenance
from utils.exceptions import ValidationError


def test_safe_metadata_actor_is_not_the_subject(monkeypatch):
    agent = uuid4()
    monkeypatch.setattr(
        "services.operation_history_writer.current_provenance", lambda: WriteProvenance(agent)
    )
    db = AsyncSession()
    row = record_operation(
        db,
        workspace_id=uuid4(),
        operation="credential.issued",
        actor_user_id="admin",
        subject_user_id="member",
        target_id="akey_safeid",
        changed_fields=["credential", "api_key=secret", "raw_query", "content"],
    )
    assert row.actor_user_id == "admin"
    assert row.subject_user_id == "member"
    assert row.agent_id == agent
    assert row.changed_fields == ["content", "credential"]
    assert row in db.new
    assert "metadata" not in OperationHistory.__table__.columns


def test_same_source_is_queued_once_and_system_does_not_inherit_agent(monkeypatch):
    monkeypatch.setattr(
        "services.operation_history_writer.current_provenance", lambda: WriteProvenance(uuid4())
    )
    db = AsyncSession()
    params = {
        "workspace_id": uuid4(),
        "operation": "sleep.completed",
        "actor_user_id": None,
        "surface": "system",
        "event_key": "report:1",
        "count": 20,
    }
    row = record_operation(db, **params)
    assert record_operation(db, **params) is row
    assert len(db.new) == 1
    assert row.agent_id is None
    assert row.count == 20


def test_private_transition_does_not_publish_old_private_activity():
    context = Context(id=uuid4(), workspace_id=uuid4(), created_by="creator", is_private=False)
    row = record_operation(
        AsyncSession(),
        operation="context.updated",
        actor_user_id="creator",
        context=context,
        private_before=True,
    )
    assert row.context_private is True
    assert row.context_owner_id == "creator"


@pytest.mark.parametrize(
    "changes",
    [{"operation": "search"}, {"surface": "untrusted"}, {"count": 0}, {"outcome": "maybe"}],
)
def test_unknown_vocabulary_is_rejected(changes):
    args = {
        "workspace_id": uuid4(),
        "operation": "memory.created",
        "actor_user_id": "user",
    } | changes
    with pytest.raises(ValueError):
        record_operation(AsyncSession(), **args)


def test_unscoped_legacy_data_is_not_guessed_into_a_workspace():
    db = AsyncSession()
    assert record_operation(db, operation="memory.created", actor_user_id="user") is None
    assert not db.new


@pytest.mark.parametrize(
    "plan,enabled",
    [("free", False), ("basic", False), ("pro", True), ("promax", True), ("unknown", False)],
)
def test_history_is_an_enforced_pro_and_max_entitlement(plan, enabled):
    assert has_feature(plan, "operation_history") is enabled
    assert feature_enforcement("operation_history") == FeatureEnforcement.ENFORCED


def test_entitled_plans_follow_the_live_registry(monkeypatch):
    from dataclasses import replace

    from config.plan_tiers import PLAN_TIERS, get_required_plan_for_feature, plans_with_feature

    assert get_required_plan_for_feature("operation_history") == "pro"
    assert plans_with_feature("operation_history") == ("pro", "promax")
    monkeypatch.setitem(
        PLAN_TIERS, "custom", replace(PLAN_TIERS["promax"], name="custom", display_name="Team")
    )
    monkeypatch.setitem(
        PLAN_TIERS, "pro", replace(PLAN_TIERS["pro"], features=frozenset({"secret_store"}))
    )
    assert plans_with_feature("operation_history") == ("promax", "custom")


def test_queueing_does_not_scan_pending_objects(monkeypatch):
    """A bulk path queues thousands of events before one flush (#2739 review)."""
    from sqlalchemy.orm import Session

    db = AsyncSession()
    monkeypatch.setattr(
        Session, "new", property(lambda _self: pytest.fail("session.new scanned per event"))
    )
    workspace = uuid4()
    rows = [
        record_operation(
            db,
            workspace_id=workspace,
            operation="memory.created",
            actor_user_id="user",
            event_key=f"bulk:{index}",
        )
        for index in range(2000)
    ]
    again = record_operation(
        db,
        workspace_id=workspace,
        operation="memory.created",
        actor_user_id="user",
        event_key="bulk:7",
    )
    assert again is rows[7]
    assert len({row.id for row in rows}) == 2000


def test_cursor_is_timezone_aware_and_stable():
    at, identifier = datetime(2026, 11, 9, tzinfo=UTC), uuid4()
    assert decode_cursor(encode_cursor(at, identifier)) == (at, identifier)


def test_custom_tier_uses_its_entitlement_not_its_display_label(monkeypatch):
    from dataclasses import replace

    from config.plan_tiers import PLAN_PROMAX, PLAN_TIERS

    monkeypatch.setitem(
        PLAN_TIERS, "custom", replace(PLAN_PROMAX, name="custom", display_name="Enterprise")
    )
    assert has_feature("custom", "operation_history")
    monkeypatch.setitem(
        PLAN_TIERS, "custom", replace(PLAN_PROMAX, name="custom", features=frozenset())
    )
    assert not has_feature("custom", "operation_history")


@pytest.mark.parametrize("cursor", ["garbage", "x" * 513, "bnVsbA==", "e30=", "WzEsMl0="])
def test_invalid_cursor_is_a_validation_error(cursor):
    with pytest.raises(ValidationError):
        decode_cursor(cursor)


@pytest.mark.parametrize("value", [2, None, {}, [], True])
def test_cursor_with_valid_date_and_non_string_id_is_rejected(value):
    import base64
    import json

    cursor = base64.urlsafe_b64encode(json.dumps(["2026-11-09T00:00:00Z", value]).encode()).decode()
    with pytest.raises(ValidationError):
        decode_cursor(cursor)
