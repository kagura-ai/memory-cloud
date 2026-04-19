"""Writer invariant tests for Issue #390 Phase 2.

The ``models/resource.py`` module registers ``before_insert`` event
listeners on all four satellite models to raise ``IntegrityError`` when
an insert would create a row with ``resource_id`` set but
``resource_pk`` left NULL. These tests pin the invariant so a future
writer that forgets to populate ``resource_pk`` is caught at test time
rather than silently producing orphan rows that reintroduce the CWE-639
slug-reuse leak vector.

The listeners live at the ORM layer, not at the DB layer — a PostgreSQL
CHECK constraint is intentionally deferred to Phase C (#325) so the
writer observation window between Phase 2 and Phase C can detect the
"zero new NULL rows" signal without a hard schema gate causing
hypothetical production errors during the rollout.

Tests call the listener function directly (via module import) rather
than going through SQLAlchemy's ``dispatch`` — the dispatch path expects
an internal ``InstanceState`` wrapper and cannot be invoked from outside
a live session. Direct function invocation is still a faithful pin: the
listener is what runs during INSERT in production, and this is the same
callable.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from models.resource import (
    IndexerState,
    ResourceEvent,
    ResourceSchema,
    ResourceToken,
    _enforce_resource_pk_invariant,
)


class TestResourcePkInvariantRejectsMissingPk:
    """Each satellite model with resource_id but no resource_pk must raise."""

    def test_resource_event_requires_resource_pk(self):
        event = ResourceEvent(
            resource_id="test_slug",
            op="upsert",
            doc_id="d1",
            version=1,
            payload={"k": "v"},
        )
        with pytest.raises(IntegrityError, match="resource_pk must be populated"):
            _enforce_resource_pk_invariant(None, None, event)

    def test_resource_schema_requires_resource_pk(self):
        schema = ResourceSchema(
            resource_id="test_slug",
            schema_version=1,
            field_definitions=[],
        )
        with pytest.raises(IntegrityError, match="resource_pk must be populated"):
            _enforce_resource_pk_invariant(None, None, schema)

    def test_indexer_state_requires_resource_pk(self):
        state = IndexerState(
            resource_id="test_slug",
            context_id=uuid.uuid4(),
            last_offset=0,
            job_status="idle",
        )
        with pytest.raises(IntegrityError, match="resource_pk must be populated"):
            _enforce_resource_pk_invariant(None, None, state)

    def test_resource_token_requires_resource_pk(self):
        token = ResourceToken(
            resource_id="test_slug",
            token_hash="x" * 64,
            quota_events_per_hour=1000,
        )
        with pytest.raises(IntegrityError, match="resource_pk must be populated"):
            _enforce_resource_pk_invariant(None, None, token)


class TestResourcePkInvariantAcceptsValidInsert:
    """Happy path: resource_pk populated means the listener is a no-op."""

    def test_resource_event_with_resource_pk_passes(self):
        event = ResourceEvent(
            resource_pk=uuid.uuid4(),
            resource_id="test_slug",
            op="upsert",
            doc_id="d1",
            version=1,
            payload={"k": "v"},
        )
        _enforce_resource_pk_invariant(None, None, event)

    def test_resource_schema_with_resource_pk_passes(self):
        schema = ResourceSchema(
            resource_pk=uuid.uuid4(),
            resource_id="test_slug",
            schema_version=1,
            field_definitions=[],
        )
        _enforce_resource_pk_invariant(None, None, schema)

    def test_indexer_state_with_resource_pk_passes(self):
        state = IndexerState(
            resource_pk=uuid.uuid4(),
            resource_id="test_slug",
            context_id=uuid.uuid4(),
            last_offset=0,
            job_status="idle",
        )
        _enforce_resource_pk_invariant(None, None, state)

    def test_resource_token_with_resource_pk_passes(self):
        token = ResourceToken(
            resource_pk=uuid.uuid4(),
            resource_id="test_slug",
            workspace_id=uuid.uuid4(),
            token_hash="y" * 64,
            quota_events_per_hour=1000,
        )
        _enforce_resource_pk_invariant(None, None, token)


class TestResourcePkInvariantEventListenerRegistration:
    """Pin the event wiring itself, not just the listener function body.

    Copilot catch on PR #392 loop 2: calling ``_enforce_resource_pk_invariant``
    directly verifies the function's behavior but would still pass if the
    ``event.listen(...)`` wiring at the bottom of ``models/resource.py`` were
    accidentally removed — producing the silent orphan-row regression this
    module is supposed to prevent. Asserting ``event.contains(...)`` locks the
    wiring at test time.
    """

    @pytest.mark.parametrize(
        "model",
        [ResourceEvent, ResourceSchema, IndexerState, ResourceToken],
    )
    def test_before_insert_listener_is_registered(self, model):
        """Every satellite model must carry the _enforce_resource_pk_invariant hook."""
        assert event.contains(model, "before_insert", _enforce_resource_pk_invariant), (
            f"{model.__name__} is missing the 'before_insert' invariant listener. "
            "The event.listen(...) loop at the bottom of models/resource.py must "
            "register _enforce_resource_pk_invariant on every satellite model."
        )
