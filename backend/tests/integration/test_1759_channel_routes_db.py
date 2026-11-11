"""Per-channel destination contexts of a chat connector (#1759), on Postgres.

The settings PATCH (both destination checks, full replacement, the flag, the
optimistic lock, the operation history), the read models, what the worker is
vended (live / soft-deleted / hard-deleted destinations), the two foreign
keys' ON DELETE, the migration round trip, and the connector write-target set
on the three deletion paths with a context that is a write target ONLY
through a route.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException, Response
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from models.auth import Context, Workspace
from models.operation_history import OperationHistory
from models.resource import WorkspaceConnector, WorkspaceConnectorChannelRoute
from services.connector_channel_routes import effective_channel_routing
from services.connector_provisioning import ConnectorProvisioningService
from utils.datetime import utcnow
from utils.exceptions import (
    ConflictError,
    NotFoundException,
    ValidationError,
    WorkerConnectorNotReadyError,
)

from ._connector_world import _live_connector_key


@pytest.fixture(autouse=True)
def _routing_enabled(monkeypatch):
    """The deployment flag on, unless a test turns it off."""
    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "connector_channel_routing_enabled", True)


async def _context(
    db: AsyncSession, workspace: Workspace, creator: str, *, private: bool = False
) -> UUID:
    ctx = Context(
        id=uuid4(),
        name=f"ctx-{uuid4().hex[:8]}",
        workspace_id=workspace.id,
        created_by=creator,
        is_private=private,
    )
    db.add(ctx)
    await db.flush()
    return ctx.id


class _Setup:
    """Owner O, admin A; A provisioned the connector, so its key acts as A.

    ``shared``: a shared context of O's; ``owners_private``: O's private
    context (A can neither read nor write it); ``admins_private``: A's."""

    def __init__(self, world, db: AsyncSession) -> None:
        self.world = world
        self.db = db

    async def build(self) -> _Setup:
        self.owner = await self.world.user("owner")
        self.admin = await self.world.user("admin")
        self.workspace = await self.world.workspace(self.owner, **{self.admin: WorkspaceRole.ADMIN})
        self.workspace_id = self.workspace.id
        self.connector = (await self.world.connector(self.workspace, by=self.admin)).connector
        self.connector_id = self.connector.id
        self.default_context = self.connector.context_id
        self.shared = await _context(self.db, self.workspace, self.owner)
        self.owners_private = await _context(self.db, self.workspace, self.owner, private=True)
        self.admins_private = await _context(self.db, self.workspace, self.admin, private=True)
        await self.db.commit()
        assert (await _live_connector_key(self.db, self.connector_id)).user_id == self.admin
        return self

    async def patch(self, caller: str | None = None, **fields):
        result = await ConnectorProvisioningService(self.db).update_connector_settings(
            workspace_id=self.workspace_id,
            connector_id=self.connector_id,
            user_id=caller or self.admin,
            **fields,
        )
        await self.db.commit()
        return result

    async def stored(self) -> set[tuple[str, str, UUID | None]]:
        rows = await self.db.execute(
            select(
                WorkspaceConnectorChannelRoute.channel_id,
                WorkspaceConnectorChannelRoute.action,
                WorkspaceConnectorChannelRoute.context_id,
            ).where(WorkspaceConnectorChannelRoute.connector_id == self.connector_id)
        )
        return {tuple(r) for r in rows.all()}  # type: ignore[misc]

    async def history(self) -> list[list[str]]:
        rows = await self.db.execute(
            select(OperationHistory.changed_fields)
            .where(
                OperationHistory.target_id == str(self.connector_id),
                OperationHistory.operation == "integration.updated",
            )
            .order_by(OperationHistory.occurred_at)
        )
        return [list(r) for r in rows.scalars()]

    async def version(self) -> int:
        return await self.db.scalar(
            select(WorkspaceConnector.config_version).where(
                WorkspaceConnector.id == self.connector_id
            )
        )


@pytest_asyncio.fixture
async def setup(connector_world, db_session) -> _Setup:
    return await _Setup(connector_world, db_session).build()


# ------------------------------------------------------------ settings PATCH


@pytest.mark.asyncio
async def test_routes_are_stored_read_back_and_replaced_as_a_whole(setup: _Setup):
    s = setup
    result = await s.patch(
        channel_routes=[
            {"channel_id": "C01", "context_id": s.shared},
            {"channel_id": "C02", "action": "skip"},
            {"channel_id": "C03", "context_id": s.admins_private},
        ],
        unrouted_private_channels="skip",
    )
    assert await s.stored() == {
        ("C01", "route", s.shared),
        ("C02", "skip", None),
        ("C03", "route", s.admins_private),
    }
    assert result.unrouted_private_channels == "skip"
    views = {v.channel_id: v for v in result.channel_routes}
    assert views["C01"].status == "ok" and views["C01"].context_is_private is False
    assert views["C01"].context_name is not None
    assert views["C03"].context_is_private is True
    assert views["C02"].action == "skip"

    # Full replacement: only what the second PATCH names is left.
    await s.patch(channel_routes=[{"channel_id": "C09", "action": "skip"}])
    assert await s.stored() == {("C09", "skip", None)}

    # The list read model carries them too.
    (item,) = [
        i
        for i in await ConnectorProvisioningService(s.db).list_connectors(s.workspace_id)
        if i.connector.id == s.connector_id
    ]
    assert [(v.channel_id, v.action) for v in item.channel_routes] == [("C09", "skip")]
    assert item.unrouted_private_channels == "skip"

    # [] and null both remove them all.
    await s.patch(channel_routes=[])
    assert await s.stored() == set()
    await s.patch(channel_routes=[{"channel_id": "C09", "action": "skip"}])
    await s.patch(channel_routes=None)
    assert await s.stored() == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["unknown", "other_workspace", "soft_deleted", "unreadable"])
async def test_a_destination_the_caller_cannot_read_is_the_uniform_not_found(
    setup: _Setup, connector_world, which
):
    s = setup
    if which == "unknown":
        target = uuid4()
    elif which == "other_workspace":
        other = await connector_world.workspace(s.admin)
        target = await _context(s.db, other, s.admin)
        await s.db.commit()
    elif which == "soft_deleted":
        target = await _context(s.db, s.workspace, s.owner)
        await s.db.execute(update(Context).where(Context.id == target).values(deleted_at=utcnow()))
        await s.db.commit()
    else:
        target = s.owners_private  # exists; the admin caller cannot see it
    before = await s.version()

    with pytest.raises(NotFoundException) as exc:
        await s.patch(channel_routes=[{"channel_id": "C01", "context_id": target}])
    await s.db.rollback()

    assert exc.value.error_code == "RES-001"
    assert str(exc.value) == str(NotFoundException("Context", str(target)))
    assert await s.stored() == set()
    assert await s.version() == before


@pytest.mark.asyncio
async def test_a_destination_the_key_owner_cannot_write_is_refused_naming_the_channel(
    setup: _Setup,
):
    """The owner (caller) can read their own private context, but the
    connector writes as the admin who provisioned it, who cannot."""
    s = setup
    with pytest.raises(ValidationError) as exc:
        await s.patch(
            caller=s.owner,
            channel_routes=[
                {"channel_id": "C01", "context_id": s.shared},
                {"channel_id": "C0PRIV", "context_id": s.owners_private},
            ],
        )
    await s.db.rollback()
    assert exc.value.details["field"] == "channel_routes"
    assert "C0PRIV" in str(exc.value)
    assert "key's owner" in str(exc.value)
    assert await s.stored() == set()


@pytest.mark.asyncio
async def test_the_caller_check_comes_before_the_key_owner_check(setup: _Setup):
    """Item order does not matter: every destination's read check runs before
    the lock, then the key-owner checks."""
    s = setup
    with pytest.raises(NotFoundException):
        await s.patch(
            caller=s.owner,
            channel_routes=[
                {"channel_id": "C0PRIV", "context_id": s.owners_private},  # key-owner fail
                {"channel_id": "C02", "context_id": uuid4()},  # caller fail
            ],
        )
    await s.db.rollback()


@pytest.mark.asyncio
async def test_a_connector_whose_key_row_is_gone_takes_no_routes(setup: _Setup):
    from models.auth import APIKey

    s = setup
    await s.db.execute(delete(APIKey).where(APIKey.connector_id == s.connector_id))
    await s.db.commit()
    with pytest.raises(ValidationError) as exc:
        await s.patch(channel_routes=[{"channel_id": "C01", "context_id": s.shared}])
    await s.db.rollback()
    assert exc.value.details["field"] == "channel_routes"


@pytest.mark.asyncio
async def test_flag_off_refuses_creation_and_allows_clearing(setup: _Setup, monkeypatch):
    from config.settings import get_settings

    s = setup
    await s.patch(
        channel_routes=[{"channel_id": "C01", "context_id": s.shared}],
        unrouted_private_channels="skip",
    )
    monkeypatch.setattr(get_settings(), "connector_channel_routing_enabled", False)

    with pytest.raises(ValidationError) as exc:
        await s.patch(channel_routes=[{"channel_id": "C02", "action": "skip"}])
    await s.db.rollback()
    assert "not enabled on this deployment" in str(exc.value)
    with pytest.raises(ValidationError):
        await s.patch(unrouted_private_channels="skip")
    await s.db.rollback()
    assert await s.stored() == {("C01", "route", s.shared)}

    # Stored routes still apply with the flag off.
    connector = await s.db.get(WorkspaceConnector, s.connector_id)
    assert (await effective_channel_routing(s.db, connector)).uses_routing is True

    result = await s.patch(channel_routes=[], unrouted_private_channels="default")
    assert await s.stored() == set()
    assert result.unrouted_private_channels == "default"


@pytest.mark.asyncio
async def test_a_stale_version_conflicts_and_changes_nothing(setup: _Setup):
    s = setup
    version = await s.version()
    with pytest.raises(ConflictError):
        await s.patch(
            channel_routes=[{"channel_id": "C01", "action": "skip"}],
            expected_config_version=version - 1,
        )
    await s.db.rollback()
    assert await s.stored() == set()
    result = await s.patch(
        channel_routes=[{"channel_id": "C01", "action": "skip"}],
        expected_config_version=version,
    )
    assert result.config_version == version + 1


@pytest.mark.asyncio
async def test_history_names_channel_routes_only_when_they_changed(setup: _Setup):
    s = setup
    await s.patch(channel_routes=[{"channel_id": "C01", "context_id": s.shared}])
    await s.patch(channel_routes=[{"channel_id": "C01", "context_id": s.shared}])  # no-op
    await s.patch(unrouted_private_channels="skip")
    await s.patch(channel_routes=[], channel_ids=["C77"])
    assert await s.history() == [
        ["channel_routes"],
        ["channel_routes"],
        ["channel_routes", "settings"],
    ]
    # Field names only: no context name or channel id in the evidence.
    rows = (
        await s.db.execute(
            select(OperationHistory).where(OperationHistory.target_id == str(s.connector_id))
        )
    ).scalars()
    for row in rows:
        assert "C01" not in repr(row.changed_fields)


# --------------------------------------------------------------- read model


@pytest.mark.asyncio
async def test_a_deleted_destination_reads_as_missing(setup: _Setup):
    s = setup
    soft = await _context(s.db, s.workspace, s.owner)
    hard = await _context(s.db, s.workspace, s.owner)
    await s.db.commit()
    await s.patch(
        channel_routes=[
            {"channel_id": "C01", "context_id": soft},
            {"channel_id": "C02", "context_id": hard},
        ]
    )
    await s.db.execute(update(Context).where(Context.id == soft).values(deleted_at=utcnow()))
    await s.db.execute(delete(Context).where(Context.id == hard))
    await s.db.commit()

    # ON DELETE SET NULL keeps the row (and the CHECK allows a route without one).
    assert await s.stored() == {("C01", "route", soft), ("C02", "route", None)}
    (item,) = [
        i
        for i in await ConnectorProvisioningService(s.db).list_connectors(s.workspace_id)
        if i.connector.id == s.connector_id
    ]
    views = {v.channel_id: v for v in item.channel_routes}
    assert views["C01"].status == views["C02"].status == "destination_missing"
    assert views["C01"].context_name is None


# ---------------------------------------------------------------- the vend


@pytest.mark.asyncio
async def test_effective_routing_skips_soft_and_hard_deleted_destinations(setup: _Setup):
    s = setup
    await s.patch(channel_routes=[{"channel_id": "C01", "context_id": s.shared}])
    connector = await s.db.get(WorkspaceConnector, s.connector_id)
    live = await effective_channel_routing(s.db, connector)
    assert live.vend_routes() == [{"channel_id": "C01", "action": "route", "context_id": s.shared}]

    await s.db.execute(update(Context).where(Context.id == s.shared).values(deleted_at=utcnow()))
    await s.db.commit()
    soft = await effective_channel_routing(s.db, connector)
    assert soft.vend_routes() == [{"channel_id": "C01", "action": "skip"}]

    await s.db.execute(delete(Context).where(Context.id == s.shared))
    await s.db.commit()
    hard = await effective_channel_routing(s.db, connector)
    assert hard.vend_routes() == [{"channel_id": "C01", "action": "skip"}]
    assert live.digest() != soft.digest() == hard.digest()


@pytest.mark.asyncio
async def test_the_vend_end_to_end(setup: _Setup):
    """A routed connector is a 404 for a worker without the capability and a
    200 carrying the routes for one with it; a later deletion of the
    destination changes the revision with no config_version bump."""
    from api.routes.workers import get_worker_config

    s = setup
    team = f"T{uuid4().hex[:10].upper()}"
    connector = await s.db.get(WorkspaceConnector, s.connector_id)
    connector.external_team_id = team
    connector.set_llm_config({"provider": "p", "model": "m", "api_key": "k"})
    await s.db.commit()

    async def vend(capabilities=None, if_none_match=None):
        return await get_worker_config(
            response=Response(),
            platform="slack",
            team_id=team,
            app_key=None,
            capabilities=capabilities,
            if_none_match=if_none_match,
            _=None,
            db=s.db,
        )

    plain = await vend()
    assert "channel_routes" not in plain.model_dump(mode="json")

    await s.patch(channel_routes=[{"channel_id": "C01", "context_id": s.shared}])
    with pytest.raises(WorkerConnectorNotReadyError):
        await vend()
    with pytest.raises(WorkerConnectorNotReadyError):
        await vend(if_none_match=f'"{plain.config_revision}"')

    routed = await vend("channel_routes")
    assert routed.model_dump(mode="json")["channel_routes"] == [
        {"channel_id": "C01", "action": "route", "context_id": str(s.shared)}
    ]
    cached = await vend("channel_routes", if_none_match=f'"{routed.config_revision}"')
    assert isinstance(cached, Response) and cached.status_code == 304

    version = await s.version()
    await s.db.execute(update(Context).where(Context.id == s.shared).values(deleted_at=utcnow()))
    await s.db.commit()
    after = await vend("channel_routes", if_none_match=f'"{routed.config_revision}"')
    assert not isinstance(after, Response)
    assert after.model_dump(mode="json")["channel_routes"] == [
        {"channel_id": "C01", "action": "skip"}
    ]
    assert await s.version() == version


# ------------------------------------------------------------ ON DELETE


@pytest.mark.asyncio
async def test_routes_go_with_their_connector(setup: _Setup):
    s = setup
    await s.patch(channel_routes=[{"channel_id": "C01", "action": "skip"}])
    await s.db.execute(delete(WorkspaceConnector).where(WorkspaceConnector.id == s.connector_id))
    await s.db.commit()
    assert await s.stored() == set()


@pytest.mark.asyncio
async def test_the_skip_check_holds_in_the_database(setup: _Setup):
    from sqlalchemy.exc import IntegrityError

    s = setup
    s.db.add(
        WorkspaceConnectorChannelRoute(
            connector_id=s.connector_id, channel_id="C01", action="skip", context_id=s.shared
        )
    )
    with pytest.raises(IntegrityError):
        await s.db.flush()
    await s.db.rollback()
    s.db.add(
        WorkspaceConnectorChannelRoute(connector_id=s.connector_id, channel_id="C01", action="x")
    )
    with pytest.raises(IntegrityError):
        await s.db.flush()
    await s.db.rollback()
    with pytest.raises(IntegrityError):
        await s.db.execute(
            update(WorkspaceConnector)
            .where(WorkspaceConnector.id == s.connector_id)
            .values(unrouted_private_channels="maybe")
        )
    await s.db.rollback()


# ----------------------------------- write targets through a route only


async def _route_to(db: AsyncSession, connector_id: UUID, context_id: UUID) -> None:
    """A route row as the PATCH stores it (its validation is tested above)."""
    db.add(
        WorkspaceConnectorChannelRoute(
            connector_id=connector_id, channel_id="C0ROUTE", action="route", context_id=context_id
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_user_deletion_hands_a_route_destination_to_the_workspace_owner(
    connector_world, db_session
):
    """The departing admin's private context is a write target only through a
    route: it is not deleted with them as personal, and it goes to the
    workspace's owner like a default context (#2420)."""
    from api.routes.admin import UserDeleteRequest, delete_user
    from services.user_deletion_service import CONNECTOR_CONTEXT_FOLLOWS_OWNER

    world = connector_world
    target = await world.user("departing")
    owner = await world.user("owner")
    member = await world.user("member")
    workspace = await world.workspace(owner, **{target: "admin", member: "admin"})
    connector = (await world.connector(workspace, by=owner)).connector
    routed = await _context(db_session, workspace, target, private=True)
    await _route_to(db_session, connector.id, routed)
    await db_session.commit()

    with pytest.raises(HTTPException) as error:
        await delete_user(
            target,
            {"user_id": "operator"},
            db_session,
            UserDeleteRequest(context_transfers={routed: member}),
        )
    assert error.value.status_code == 409
    assert error.value.detail == CONNECTOR_CONTEXT_FOLLOWS_OWNER
    await db_session.rollback()

    response = await delete_user(target, {"user_id": "operator"}, db_session)
    assert response["deleted"]["contexts"] == 0
    db_session.expire_all()
    assert await db_session.scalar(select(Context.created_by).where(Context.id == routed)) == owner


@pytest.mark.asyncio
async def test_a_linked_contributor_does_not_take_a_route_destination(connector_world, db_session):
    from api.routes.admin import delete_user
    from models.auth import IdentityLink
    from models.memory import Memory

    world = connector_world
    target = await world.user("departing")
    linked = await world.user("linked")
    owner = await world.user("owner")
    workspace = await world.workspace(owner, **{target: "admin", linked: "admin"})
    connector = (await world.connector(workspace, by=owner)).connector
    routed = await _context(db_session, workspace, target, private=True)
    await _route_to(db_session, connector.id, routed)
    group = uuid4()
    db_session.add_all(
        [
            IdentityLink(group_id=group, user_id=target, linked_by=target),
            IdentityLink(group_id=group, user_id=linked, linked_by=target),
        ]
    )
    db_session.add(
        Memory(
            id=uuid4(),
            user_id=linked,
            workspace_id=workspace.id,
            context_id=routed,
            content="theirs",
            summary="theirs",
            type="note",
            client="test",
        )
    )
    await db_session.commit()

    await delete_user(target, {"user_id": "operator"}, db_session)
    db_session.expire_all()
    assert await db_session.scalar(select(Context.created_by).where(Context.id == routed)) == owner


@pytest.mark.asyncio
async def test_account_erasure_reowns_a_route_destination(connector_world, db_session):
    """Erasure: the identity-link hand-over leaves it alone and the re-own
    hands it to the workspace's owner before the authorship sweep."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from models.auth import IdentityLink
    from models.memory import Memory

    world = connector_world
    target = await world.user("target")
    linked = await world.user("linked")
    owner = f"owner_{uuid4().hex[:8]}"
    workspace = await world.workspace(
        owner, **{target: WorkspaceRole.ADMIN, linked: WorkspaceRole.ADMIN}
    )
    connector = (await world.connector(workspace, by=owner)).connector
    routed = await _context(db_session, workspace, target, private=True)
    await _route_to(db_session, connector.id, routed)
    db_session.add_all(
        [
            IdentityLink(group_id=(group := uuid4()), user_id=target, linked_by=target),
            IdentityLink(group_id=group, user_id=linked, linked_by=target),
            Memory(
                id=uuid4(),
                user_id=linked,
                workspace_id=workspace.id,
                context_id=routed,
                content="theirs",
                summary="theirs",
                type="note",
                client="test",
            ),
        ]
    )
    await db_session.flush()

    with (
        patch(
            "services.account_erasure_service.delete_user_points",
            new=AsyncMock(return_value={"kagura_memories": 0}),
        ),
        patch(
            "services.account_erasure_service.clear_co_activations",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "services.account_erasure_service.clear_user_rate_limits",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "services.account_erasure_service.get_redis_client",
            new=MagicMock(return_value=MagicMock(setex=AsyncMock(), delete=AsyncMock())),
        ),
        patch("services.account_erasure_service.get_session_manager", new=lambda: None),
        patch(
            "services.security_notification_service.purge_user_notification_state",
            AsyncMock(return_value=0),
        ),
    ):
        counts = await world.erase(target)

    assert await db_session.scalar(select(Context.created_by).where(Context.id == routed)) == owner
    assert counts["connector_contexts_reowned"] == 1
    assert counts["contexts_handed_over"] == 0


# ------------------------------------------------------------ migration


def test_f35_round_trip_and_the_guarded_downgrade() -> None:
    from tests.integration.test_alembic_migrations import (
        TestB03NeuralEdgesBackfillMigration as _B03,
    )
    from tests.integration.test_alembic_migrations import (
        _downgrade_to,
        _reset_alembic_state,
        _sync_engine,
        _upgrade_to,
    )

    prior = "f34_2739_history_retention_addon"
    _reset_alembic_state()
    engine = _sync_engine()

    def table_exists(conn) -> bool:
        return (
            conn.execute(text("SELECT to_regclass('workspace_connector_channel_routes')")).scalar()
            is not None
        )

    def column_default(conn):
        return conn.execute(
            text(
                "SELECT column_default, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'workspace_connectors' "
                "AND column_name = 'unrouted_private_channels'"
            )
        ).first()

    try:
        _upgrade_to(prior)
        with engine.connect() as conn:
            assert not table_exists(conn)
            assert column_default(conn) is None
        _upgrade_to("f35_1759_channel_routes")
        with engine.connect() as conn:
            assert table_exists(conn)
            default, nullable = column_default(conn)
            assert "default" in default and nullable == "NO"
            checks = set(
                conn.execute(
                    text(
                        "SELECT conname FROM pg_constraint WHERE convalidated AND conname IN "
                        "('check_workspace_connectors_unrouted_private_channels', "
                        "'check_workspace_connector_channel_routes_action', "
                        "'check_workspace_connector_channel_routes_skip_no_context')"
                    )
                ).scalars()
            )
            assert len(checks) == 3
            fks = dict(
                conn.execute(
                    text(
                        "SELECT conname, confdeltype FROM pg_constraint WHERE conrelid = "
                        "'workspace_connector_channel_routes'::regclass AND contype = 'f'"
                    )
                ).all()
            )
            assert fks == {
                "fk_workspace_connector_channel_routes_connector_id": "c",
                "fk_workspace_connector_channel_routes_context_id": "n",
            }

        # A connector with the switch on blocks the downgrade.
        with engine.begin() as conn:
            ws, _ = _B03._seed_workspace_and_context(conn, "f35-owner")
            conn.execute(
                text(
                    "INSERT INTO resources (id, workspace_id, resource_id, name) "
                    "VALUES (:r, :w, 'f35-res', 'f35')"
                ),
                {"r": (res := uuid4()), "w": ws},
            )
            conn.execute(
                text(
                    "INSERT INTO workspace_connectors "
                    "(id, resource_pk, workspace_id, connector_type, unrouted_private_channels) "
                    "VALUES (:c, :r, :w, 'slack', 'skip')"
                ),
                {"c": (cid := uuid4()), "r": res, "w": ws},
            )
        with pytest.raises(RuntimeError, match="downgrade refused"):
            _downgrade_to(prior)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE workspace_connectors SET unrouted_private_channels = 'default' "
                    "WHERE id = :c"
                ),
                {"c": cid},
            )
        _downgrade_to(prior)
        with engine.connect() as conn:
            assert not table_exists(conn)
            assert column_default(conn) is None
        # Re-run: the upgrade's steps before the VALIDATE commit are re-runnable.
        _upgrade_to("f35_1759_channel_routes")
        with engine.connect() as conn:
            assert table_exists(conn)
    finally:
        try:
            _upgrade_to("head")
        finally:
            try:
                with engine.begin() as conn:
                    conn.execute(text("DELETE FROM workspaces WHERE owner_user_id = 'f35-owner'"))
            finally:
                engine.dispose()
