"""Paid-only features are suspended, never deleted, on a plan without them (#1939).

The predicates read the workspace's EFFECTIVE limits, not ``has_feature``:
feature flags gate creation only, and Basic / Pro keep positive serve-only
limits for objects made on a higher plan (#1551). Suspension is the limit
dropping to zero — the workspace fell back to Free.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from config.constants import GATE_PLAN
from models.auth import Context, User, Workspace
from models.resource import Resource, ResourceToken, WorkspaceConnector
from services import plan_suspension
from services.plan_suspension import (
    SUSPENDED_CONNECTORS,
    SUSPENDED_PUBLIC,
    SUSPENDED_RESOURCES,
    SUSPENDED_SLEEP,
    connectors_suspended,
    ensure_ingest_allowed,
    ensure_public_serving_allowed,
    ingest_suspended,
    public_suspended,
    resources_suspended,
    sleep_suspended,
    suspended_features,
)
from utils.exceptions import FeatureNotAvailableError, QuotaExceededError


def _ws(plan: str, **addons: int) -> Workspace:
    return Workspace(id=uuid4(), name="w", plan_name=plan, owner_user_id="u", **addons)


class TestPredicates:
    def test_free_suspends_every_paid_only_feature(self) -> None:
        ws = _ws("free")
        assert connectors_suspended(ws)
        assert resources_suspended(ws)
        assert sleep_suspended(ws)
        assert public_suspended(ws)

    def test_xl_suspends_nothing(self) -> None:
        ws = _ws("promax")
        assert not connectors_suspended(ws)
        assert not resources_suspended(ws)
        assert not sleep_suspended(ws)
        assert not public_suspended(ws)

    def test_serve_only_limits_on_basic_keep_connectors_and_resources_running(self) -> None:
        """#1551: Basic lacks the ``connectors`` / ``resources`` FLAGS but keeps
        positive limits so objects made on a higher plan keep serving. A flag
        check here would wrongly stop them."""
        ws = _ws("basic")
        assert not connectors_suspended(ws)
        assert not resources_suspended(ws)

    def test_addon_cannot_lift_a_zero_base(self) -> None:
        ws = _ws("free", addon_connector_bonus=5, addon_sleep_contexts_bonus=5)
        assert connectors_suspended(ws)
        assert sleep_suspended(ws)


class TestPublicServing:
    def test_refused_on_free_with_the_public_quota_type(self) -> None:
        with pytest.raises(QuotaExceededError) as exc:
            ensure_public_serving_allowed(_ws("free"))
        assert exc.value.status_code == 429
        assert exc.value.details["quota_type"] == "api_public_daily"
        # The rate-limit family ships no counts (#1644).
        assert "current" not in exc.value.details
        assert "limit" not in exc.value.details

    def test_allowed_on_pro(self) -> None:
        ensure_public_serving_allowed(_ws("pro"))


@pytest_asyncio.fixture(loop_scope="session")
async def seeded(db_session: AsyncSession) -> AsyncIterator[dict[str, object]]:
    """A workspace with one connector resource and one ordinary resource."""
    user_id = f"u_{uuid4().hex[:8]}"
    workspace_id = uuid4()
    db_session.add(
        User(
            email=f"{user_id}@suspension.invalid",
            user_id=user_id,
            name="Suspension User",
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
            auth_provider="google",
        )
    )
    workspace = Workspace(
        id=workspace_id,
        name=f"susp-{uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id=user_id,
        daily_api_limit=500,
        weekly_api_limit=2500,
    )
    db_session.add(workspace)
    connector_pk = uuid4()
    ordinary_pk = uuid4()
    db_session.add(Resource(id=connector_pk, workspace_id=workspace_id, resource_id="conn-res"))
    db_session.add(Resource(id=ordinary_pk, workspace_id=workspace_id, resource_id="ord-res"))
    await db_session.flush()
    db_session.add(
        WorkspaceConnector(
            resource_pk=connector_pk,
            workspace_id=workspace_id,
            connector_type="slack",
            created_by=user_id,
        )
    )
    await db_session.flush()
    yield {
        "workspace": workspace,
        "user_id": user_id,
        "connector_pk": connector_pk,
        "ordinary_pk": ordinary_pk,
    }


@pytest.mark.asyncio(loop_scope="session")
class TestIngest:
    async def test_connector_resource_refused_with_connectors_feature(self, db_session, seeded):
        with pytest.raises(FeatureNotAvailableError) as exc:
            await ensure_ingest_allowed(db_session, seeded["workspace"], seeded["connector_pk"])
        assert exc.value.status_code == 403
        assert exc.value.error_code == "FEAT-001"
        assert exc.value.details["feature"] == "connectors"
        assert exc.value.details["gate"] == GATE_PLAN

    async def test_ordinary_resource_refused_with_resources_feature(self, db_session, seeded):
        with pytest.raises(FeatureNotAvailableError) as exc:
            await ensure_ingest_allowed(db_session, seeded["workspace"], seeded["ordinary_pk"])
        assert exc.value.details["feature"] == "resources"

    async def test_unresolved_resource_is_an_ordinary_resource(self, db_session, seeded):
        with pytest.raises(FeatureNotAvailableError) as exc:
            await ensure_ingest_allowed(db_session, seeded["workspace"], None)
        assert exc.value.details["feature"] == "resources"

    async def test_paid_plan_ingests(self, db_session, seeded):
        workspace = seeded["workspace"]
        workspace.plan_name = "promax"
        await ensure_ingest_allowed(db_session, workspace, seeded["connector_pk"])
        assert not await ingest_suspended(db_session, workspace, seeded["ordinary_pk"])

    async def test_basic_keeps_connector_but_not_unrelated_kinds(self, db_session, seeded):
        workspace = seeded["workspace"]
        workspace.plan_name = "basic"
        assert not await ingest_suspended(db_session, workspace, seeded["connector_pk"])
        assert not await ingest_suspended(db_session, workspace, seeded["ordinary_pk"])

    async def test_non_raising_twin_agrees(self, db_session, seeded):
        workspace = seeded["workspace"]
        assert await ingest_suspended(db_session, workspace, seeded["connector_pk"])
        assert await ingest_suspended(db_session, workspace, seeded["ordinary_pk"])


@pytest.mark.asyncio(loop_scope="session")
class TestSuspendedFeatures:
    """Only features that are suspended AND still have something to pause are
    listed, so a Free workspace that never used them sees no banner."""

    async def test_fresh_free_workspace_lists_nothing_beyond_its_connector(
        self, db_session, seeded
    ):
        assert await suspended_features(db_session, seeded["workspace"]) == [SUSPENDED_CONNECTORS]

    async def test_lists_every_paused_feature_in_use(self, db_session, seeded):
        workspace: Workspace = seeded["workspace"]  # type: ignore[assignment]
        workspace_id: UUID = workspace.id
        db_session.add(
            ResourceToken(
                resource_pk=seeded["ordinary_pk"],
                resource_id="ord-res",
                workspace_id=workspace_id,
                token_hash=f"hash_{uuid4().hex}",
                description="t",
                created_by=seeded["user_id"],
                is_active=True,
            )
        )
        db_session.add(
            Context(
                id=uuid4(),
                workspace_id=workspace_id,
                name=f"sleepy-{uuid4().hex[:6]}",
                created_by=seeded["user_id"],
                sleep_mode="full",
                is_public=True,
            )
        )
        await db_session.flush()
        assert await suspended_features(db_session, workspace) == [
            SUSPENDED_CONNECTORS,
            SUSPENDED_RESOURCES,
            SUSPENDED_SLEEP,
            SUSPENDED_PUBLIC,
        ]

    async def test_paid_plan_lists_nothing(self, db_session, seeded):
        workspace = seeded["workspace"]
        workspace.plan_name = "promax"
        assert await suspended_features(db_session, workspace) == []


def test_vocabulary_is_stable() -> None:
    """The plan API and the web UI key copy on these strings."""
    assert plan_suspension.SUSPENDABLE_FEATURES == (
        "connectors",
        "resources",
        "sleep",
        "public",
    )
