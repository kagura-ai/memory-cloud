"""DB-backed connector provisioning flow tests (Issue #851, F6-b of #755)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.routes.resource_ingest import ingest_event, verify_resource_token
from auth.api_keys import APIKeyManager
from auth.workspace_roles import WorkspaceRole
from models.auth import User, Workspace, WorkspaceMember
from models.resource import Resource, ResourceEvent, ResourceToken, WorkspaceConnector
from models.schemas import ResourceEventRequest
from services.connector_provisioning import ConnectorProvisioningService
from utils.exceptions import NotFoundException, ValidationError


@pytest.fixture(autouse=True)
def _mock_qdrant_collection():
    """Stub out Qdrant collection creation for the auto-create-context path.

    The CI backend-integration job runs with Postgres + Redis only (no Qdrant),
    so ContextService.create_context(create_collection=True) — reached when a
    connector auto-creates its write-target context — would fail with a
    connection error. These tests exercise the provisioning DB flow, not Qdrant,
    so the collection-ensure call is mocked to a no-op.
    """
    with patch(
        "services.context_service.ContextService._ensure_context_collection",
        new=AsyncMock(return_value=None),
    ):
        yield


async def _seed_workspace(
    db: AsyncSession,
    *,
    # #1551: provisioning a connector is XL-only ("may create"); these flows
    # all CREATE, so they run on promax. Serve-path coverage for existing M/L
    # connectors lives in tests/services/test_connector_provisioning.py.
    plan_name: str = "promax",
) -> tuple[str, Workspace]:
    user_id = f"user-{uuid4().hex}"
    await _add_user(db, user_id)
    workspace = Workspace(
        name=f"ws-{uuid4().hex[:8]}",
        owner_user_id=user_id,
        plan_name=plan_name,
    )
    db.add(workspace)
    await db.flush()
    member = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=user_id,
        role=WorkspaceRole.OWNER,
    )
    db.add(member)
    await db.flush()
    return user_id, workspace


async def _add_user(db: AsyncSession, user_id: str) -> None:
    """A ``users`` row for ``user_id`` (a mint for the workspace owner
    share-locks it, #2305)."""
    from models.auth import User

    if await db.scalar(select(User.user_id).where(User.user_id == user_id)) is not None:
        return
    db.add(
        User(
            email=f"{user_id}@example.com",
            user_id=user_id,
            name=user_id,
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
            auth_provider="google",
        )
    )
    await db.flush()


class TestConnectorProvisioningDbFlow:
    @pytest.mark.asyncio
    async def test_runtime_update_is_normalized_revisioned_and_workspace_scoped(
        self,
        db_session: AsyncSession,
    ):
        user_id, workspace = await _seed_workspace(db_session)
        result = await ConnectorProvisioningService(db_session).provision_connector(
            workspace_id=workspace.id,
            user_id=user_id,
            connector_type="slack",
            resource_id=f"slack_{uuid4().hex[:8]}",
            runtime_config={"vision_enabled": False},
        )
        original_revision = result.connector.config_version

        update = await ConnectorProvisioningService(db_session).update_runtime_config(
            workspace_id=workspace.id,
            connector_id=result.connector.id,
            runtime_config={"vision_enabled": True},
            user_id=user_id,
        )

        assert update.config_version == original_revision + 1
        assert update.runtime_config["vision_enabled"] is True
        assert update.runtime_config["buffer"]["ttl_seconds"] == 86400

        other_user_id, other_workspace = await _seed_workspace(db_session)
        with pytest.raises(NotFoundException):
            await ConnectorProvisioningService(db_session).update_runtime_config(
                workspace_id=other_workspace.id,
                connector_id=result.connector.id,
                runtime_config={"vision_enabled": False},
                user_id=other_user_id,
            )

    @pytest.mark.asyncio
    async def test_token_failure_rolls_back_resource_and_connector_rows(
        self,
        db_session: AsyncSession,
    ):
        """AC1: mid-flow failure leaves no Resource or WorkspaceConnector orphan."""
        user_id, workspace = await _seed_workspace(db_session)
        resource_id = f"slack_{uuid4().hex[:8]}"

        with patch(
            "services.connector_provisioning.ResourceTokenManager.create_token",
            new=AsyncMock(side_effect=RuntimeError("token mint failed")),
        ):
            with pytest.raises(RuntimeError, match="token mint failed"):
                await ConnectorProvisioningService(db_session).provision_connector(
                    workspace_id=workspace.id,
                    user_id=user_id,
                    connector_type="slack",
                    resource_id=resource_id,
                )

        await db_session.rollback()

        resource_count = (
            await db_session.execute(
                select(func.count(Resource.id)).where(
                    Resource.workspace_id == workspace.id,
                    Resource.resource_id == resource_id,
                )
            )
        ).scalar_one()
        connector_count = (
            await db_session.execute(
                select(func.count(WorkspaceConnector.id)).where(
                    WorkspaceConnector.workspace_id == workspace.id
                )
            )
        ).scalar_one()

        assert resource_count == 0
        assert connector_count == 0

    @pytest.mark.asyncio
    async def test_provision_then_ingest_event_writes_resource_pk_and_workspace(
        self,
        db_session: AsyncSession,
    ):
        """AC5: provision connector, ingest via token, then assert event binding."""
        user_id, workspace = await _seed_workspace(db_session)
        resource_id = f"slack_{uuid4().hex[:8]}"

        result = await ConnectorProvisioningService(db_session).provision_connector(
            workspace_id=workspace.id,
            user_id=user_id,
            connector_type="slack",
            resource_id=resource_id,
        )
        await db_session.commit()

        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        auth = await verify_resource_token(
            resource_id,
            request,
            x_resource_api_key=result.plaintext_token,
            db=db_session,
        )
        event_request = ResourceEventRequest(
            op="upsert",
            doc_id="1717000000.000100",
            version=1,
            payload={"text": "hello from slack"},
            idempotency_key=f"{result.connector.id}:summary-1",
        )

        with (
            patch("api.routes.resource_ingest.check_event_quota", new=AsyncMock()),
            patch("api.routes.resource_ingest._schedule_indexer_for_resource", new=AsyncMock()),
            patch("utils.usage_logger.log_usage", new=AsyncMock()),
        ):
            response = await ingest_event(
                resource_id,
                event_request,
                auth=auth,
                db=db_session,
            )

        row = (
            await db_session.execute(
                select(ResourceEvent, Resource.workspace_id)
                .join(Resource, Resource.id == ResourceEvent.resource_pk)
                .where(ResourceEvent.id == response.event_id)
            )
        ).one()
        event, event_workspace_id = row

        assert event.resource_pk == result.resource_pk
        assert event.resource_id == resource_id
        assert event_workspace_id == workspace.id
        assert (
            await db_session.execute(
                select(func.count(ResourceToken.id)).where(
                    ResourceToken.resource_pk == result.resource_pk
                )
            )
        ).scalar_one() == 1


@pytest.mark.asyncio
async def test_registration_path_creates_context_and_mints_kmc_key(
    db_session: AsyncSession,
):
    """Spec 2026-06-02: auto_create_context_name → context bound + KMC key minted.

    The connector gets a write-target context, a workspace-scoped API key is
    minted and stored Fernet-encrypted (retrievable via get_kmc_api_key), and
    the plaintext is surfaced once in the result.
    """
    from models.auth import APIKey, Context

    user_id, workspace = await _seed_workspace(db_session, plan_name="promax")
    resource_id = f"slack_{uuid4().hex[:8]}"
    ctx_name = f"slack-{uuid4().hex[:8]}"

    result = await ConnectorProvisioningService(db_session).provision_connector(
        workspace_id=workspace.id,
        user_id=user_id,
        connector_type="slack",
        resource_id=resource_id,
        auto_create_context_name=ctx_name,
        llm_config={"provider": "anthropic", "model": "claude-haiku-4-5", "api_key": "sk-test"},
        channel_ids=["C01EXAMPLE"],
        locale="ja",
    )
    await db_session.flush()

    # Context created and bound.
    assert result.context_id is not None
    ctx = (
        await db_session.execute(select(Context).where(Context.id == result.context_id))
    ).scalar_one()
    assert ctx.name == ctx_name
    assert ctx.workspace_id == workspace.id

    # Connector carries the new config + encrypted secrets round-trip.
    assert result.connector.context_id == result.context_id
    assert result.connector.locale == "ja"
    assert result.connector.channel_ids == ["C01EXAMPLE"]
    assert result.connector.get_llm_config()["api_key"] == "sk-test"

    # KMC write key minted, surfaced once, stored encrypted (retrievable).
    assert result.plaintext_kmc_api_key
    assert result.connector.get_kmc_api_key() == result.plaintext_kmc_api_key
    kmc_key = (
        await db_session.execute(select(APIKey).where(APIKey.workspace_id == workspace.id))
    ).scalar_one()
    # #1537: the key expires by default, and the worker config endpoint's
    # expiry warning reads only the connector column — it must mirror the
    # row exactly (as rotate_kmc_key already does), not stay NULL.
    assert kmc_key.expires_at is not None
    assert result.connector.kmc_api_key_expires_at == kmc_key.expires_at
    # #1952: linked to its connector by the FK, still named after it.
    assert kmc_key.connector_id == result.connector.id
    assert kmc_key.name == f"connector:{result.connector.id}"


@pytest.mark.asyncio
async def test_provision_seeds_canonical_chat_resource_schema(db_session: AsyncSession):
    """Issue #910: connector registration provisions the canonical chat schema.

    The ai-worker writes ``payload={"text": <summary>}`` on ingest; the indexer
    only projects fields the ResourceSchema marks fulltext/vector. So a chat
    connector must get a v1 schema with a fulltext ``text`` field or the summary
    is lost from search.
    """
    from models.resource import ResourceSchema

    user_id, workspace = await _seed_workspace(db_session, plan_name="promax")
    resource_id = f"slack_{uuid4().hex[:8]}"

    result = await ConnectorProvisioningService(db_session).provision_connector(
        workspace_id=workspace.id,
        user_id=user_id,
        connector_type="slack",
        resource_id=resource_id,
    )
    await db_session.flush()

    schema = (
        await db_session.execute(
            select(ResourceSchema).where(ResourceSchema.resource_pk == result.resource_pk)
        )
    ).scalar_one()
    assert schema.schema_version == 1
    fields = {f["name"]: f for f in schema.field_definitions}
    assert "text" in fields
    text_field = fields["text"]
    assert text_field["classification"] == "public"
    assert "fulltext" in text_field["index_hint"]
    assert "vector" in text_field["index_hint"]


@pytest.mark.asyncio
async def test_ensure_chat_schema_is_idempotent(db_session: AsyncSession):
    """A second _ensure_chat_resource_schema call does not duplicate or clobber."""
    from sqlalchemy import func as _func

    from models.resource import Resource, ResourceSchema

    user_id, workspace = await _seed_workspace(db_session, plan_name="promax")
    # Create a bare resource to attach a schema to.
    res = Resource(workspace_id=workspace.id, resource_id="slack_idem", created_by=user_id)
    db_session.add(res)
    await db_session.flush()

    svc = ConnectorProvisioningService(db_session)
    await svc._ensure_chat_resource_schema(res.id, "slack_idem")
    await svc._ensure_chat_resource_schema(res.id, "slack_idem")
    await db_session.flush()

    count = (
        await db_session.execute(
            select(_func.count(ResourceSchema.id)).where(ResourceSchema.resource_pk == res.id)
        )
    ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_delete_connector_removes_kmc_key_and_connector(
    db_session: AsyncSession,
):
    """Spec 2026-06-02: delete drops the connector row and its KMC key.

    The write-target context is preserved (user data); the KMC API key row goes
    with the connector through ``api_keys.connector_id`` ON DELETE CASCADE
    (#1952), so the worker's next verify finds no key.
    """
    from models.auth import APIKey, Context

    user_id, workspace = await _seed_workspace(db_session, plan_name="promax")
    result = await ConnectorProvisioningService(db_session).provision_connector(
        workspace_id=workspace.id,
        user_id=user_id,
        connector_type="slack",
        resource_id=f"slack_{uuid4().hex[:8]}",
        auto_create_context_name=f"slack-{uuid4().hex[:8]}",
    )
    await db_session.flush()
    connector_id = result.connector.id
    context_id = result.context_id

    deleted = await ConnectorProvisioningService(db_session).delete_connector(
        workspace.id, connector_id
    )
    await db_session.flush()
    assert deleted is True

    # Connector row gone.
    remaining = (
        await db_session.execute(
            select(func.count(WorkspaceConnector.id)).where(WorkspaceConnector.id == connector_id)
        )
    ).scalar_one()
    assert remaining == 0

    # KMC key row gone with the connector (#1952 CASCADE).
    keys = (
        await db_session.execute(select(APIKey).where(APIKey.workspace_id == workspace.id))
    ).all()
    assert keys == []
    assert await APIKeyManager(db_session).verify_key(result.plaintext_kmc_api_key) is None

    # Context preserved.
    ctx_count = (
        await db_session.execute(select(func.count(Context.id)).where(Context.id == context_id))
    ).scalar_one()
    assert ctx_count == 1


@pytest.mark.asyncio
async def test_delete_connector_revokes_an_unlinked_key_of_its_name(db_session: AsyncSession):
    """#1952: a ``connector:{id}`` key the previous image minted after the f13
    backfill (blue-green / rollback window) has no link, so the cascade cannot
    reach it — ``delete_connector`` revokes it by name, in the connector's
    workspace only, and leaves every other row alone."""
    from models.auth import APIKey

    user_id, workspace = await _seed_workspace(db_session, plan_name="promax")
    _other_user, other_workspace = await _seed_workspace(db_session, plan_name="promax")
    service = ConnectorProvisioningService(db_session)
    result = await service.provision_connector(
        workspace_id=workspace.id,
        user_id=user_id,
        connector_type="slack",
        resource_id=f"slack_{uuid4().hex[:8]}",
        auto_create_context_name=f"slack-{uuid4().hex[:8]}",
    )
    await db_session.flush()
    connector_id = result.connector.id
    name = f"connector:{connector_id}"
    manager = APIKeyManager(db_session)
    # The old image's mint: same name, same workspace, no link (another
    # minter, since the name is unique per active user + workspace).
    unlinked_plain, unlinked = await manager.create_key(
        name=name, user_id=f"user-{uuid4().hex}", workspace_id=workspace.id
    )
    # Same name in another workspace: not this connector's key.
    elsewhere_plain, elsewhere = await manager.create_key(
        name=name, user_id=user_id, workspace_id=other_workspace.id
    )
    # Another key of the workspace.
    laptop_plain, laptop = await manager.create_key(
        name="laptop", user_id=user_id, workspace_id=workspace.id
    )
    unlinked_id, elsewhere_id, laptop_id = unlinked.id, elsewhere.id, laptop.id

    assert await service.delete_connector(workspace.id, connector_id) is True
    await db_session.flush()

    rows = {
        row.id: row
        for row in (
            await db_session.execute(
                select(APIKey)
                .where(APIKey.workspace_id.in_([workspace.id, other_workspace.id]))
                .execution_options(populate_existing=True)
            )
        ).scalars()
    }
    # The linked key went with the cascade; the unlinked one is revoked.
    assert set(rows) == {unlinked_id, elsewhere_id, laptop_id}
    assert rows[unlinked_id].revoked_at is not None
    assert rows[elsewhere_id].revoked_at is None
    assert rows[laptop_id].revoked_at is None
    assert await manager.verify_key(unlinked_plain) is None
    assert await manager.verify_key(elsewhere_plain) is not None
    assert await manager.verify_key(laptop_plain) is not None


@pytest.mark.asyncio
async def test_rotate_kmc_key_links_the_replacement_and_revokes_the_old_row(
    db_session: AsyncSession,
):
    """#1952: the replacement key carries ``connector_id``; the old row stays,
    revoked, and keeps its link (history)."""
    from models.auth import APIKey

    user_id, workspace = await _seed_workspace(db_session, plan_name="promax")
    service = ConnectorProvisioningService(db_session)
    result = await service.provision_connector(
        workspace_id=workspace.id,
        user_id=user_id,
        connector_type="slack",
        resource_id=f"slack_{uuid4().hex[:8]}",
        auto_create_context_name=f"slack-{uuid4().hex[:8]}",
    )
    await db_session.flush()
    connector_id = result.connector.id

    rotated = await service.rotate_kmc_key(
        workspace_id=workspace.id, connector_id=connector_id, user_id=user_id
    )
    await db_session.flush()

    rows = (
        (
            await db_session.execute(
                select(APIKey).where(APIKey.workspace_id == workspace.id).order_by(APIKey.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    old, new = rows
    assert old.connector_id == connector_id and old.revoked_at is not None
    assert new.connector_id == connector_id and new.revoked_at is None
    assert new.name == f"connector:{connector_id}"
    verified = await APIKeyManager(db_session).verify_key(rotated.plaintext_kmc_api_key)
    assert verified is not None and verified.is_connector_key is True


@pytest.mark.asyncio
async def test_rotate_kmc_key_heals_an_unlinked_backing_row(db_session: AsyncSession):
    """#1952: a stored key whose row has no link (minted by the name-only code
    after the f13 backfill ran) still rotates — matched by its hash — and the
    replacement is linked."""
    from sqlalchemy import update

    from models.auth import APIKey

    user_id, workspace = await _seed_workspace(db_session, plan_name="promax")
    service = ConnectorProvisioningService(db_session)
    result = await service.provision_connector(
        workspace_id=workspace.id,
        user_id=user_id,
        connector_type="slack",
        resource_id=f"slack_{uuid4().hex[:8]}",
        auto_create_context_name=f"slack-{uuid4().hex[:8]}",
    )
    await db_session.flush()
    connector_id = result.connector.id
    await db_session.execute(
        update(APIKey)
        .where(APIKey.connector_id == connector_id)
        .values(connector_id=None)
        .execution_options(synchronize_session="fetch")
    )

    rotated = await service.rotate_kmc_key(
        workspace_id=workspace.id, connector_id=connector_id, user_id=user_id
    )
    await db_session.flush()

    rows = (
        (
            await db_session.execute(
                select(APIKey).where(APIKey.workspace_id == workspace.id).order_by(APIKey.id)
            )
        )
        .scalars()
        .all()
    )
    old, new = rows
    assert old.connector_id is None and old.revoked_at is not None
    assert new.connector_id == connector_id and new.revoked_at is None
    assert rotated.key_name == f"connector:{connector_id}"


@pytest.mark.asyncio
async def test_rotate_kmc_key_heals_a_connector_whose_key_row_is_gone(db_session: AsyncSession):
    """#2305: the backing row was deleted (an account erasure before #2305)
    and no active key is linked to the connector — nothing to duplicate, so
    the replacement is minted under the workspace's owner."""
    from sqlalchemy import delete

    from models.auth import APIKey
    from utils.hashing import sha256_hex

    owner_id, workspace = await _seed_workspace(db_session, plan_name="promax")
    await _add_user(db_session, owner_id)
    admin_id = f"admin-{uuid4().hex}"
    db_session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=admin_id, role=WorkspaceRole.ADMIN)
    )
    await db_session.flush()
    service = ConnectorProvisioningService(db_session)
    result = await service.provision_connector(
        workspace_id=workspace.id,
        user_id=admin_id,
        connector_type="slack",
        resource_id=f"slack_{uuid4().hex[:8]}",
        auto_create_context_name=f"slack-{uuid4().hex[:8]}",
    )
    await db_session.flush()
    connector = result.connector
    version = connector.config_version
    await db_session.execute(delete(APIKey).where(APIKey.connector_id == connector.id))

    with patch("services.connector_provisioning.logger") as logger:
        rotated = await service.rotate_kmc_key(
            workspace_id=workspace.id, connector_id=connector.id, user_id=admin_id
        )
    await db_session.flush()

    assert rotated.key_owner_user_id == owner_id
    assert rotated.config_version == version + 1
    assert connector.config_version == version + 1
    (row,) = (
        (await db_session.execute(select(APIKey).where(APIKey.connector_id == connector.id)))
        .scalars()
        .all()
    )
    assert row.user_id == owner_id and row.revoked_at is None
    assert row.name == f"connector:{connector.id}"
    assert connector.get_kmc_api_key() == rotated.plaintext_kmc_api_key
    assert row.key_hash == sha256_hex(rotated.plaintext_kmc_api_key)
    assert connector.kmc_api_key_expires_at == row.expires_at
    event, fields = logger.info.call_args.args[0], logger.info.call_args.kwargs
    assert event == "connector_kmc_key_healed"
    assert fields["key_owner"] == owner_id and fields["rotated_by"] == admin_id
    assert rotated.plaintext_kmc_api_key not in repr(logger.info.call_args)


@pytest.mark.asyncio
async def test_rotate_kmc_key_fails_when_another_linked_key_is_live(db_session: AsyncSession):
    """#1952: the backing row is matched by ``key_hash`` AND ``connector_id``;
    a stored key that matches no active row while another active key is
    linked to the connector is drift — the existing ``ValidationError``, and
    nothing is minted (#2305 heals only when no linked key is live)."""
    from sqlalchemy import update

    from models.auth import APIKey

    user_id, workspace = await _seed_workspace(db_session, plan_name="promax")
    service = ConnectorProvisioningService(db_session)
    result = await service.provision_connector(
        workspace_id=workspace.id,
        user_id=user_id,
        connector_type="slack",
        resource_id=f"slack_{uuid4().hex[:8]}",
        auto_create_context_name=f"slack-{uuid4().hex[:8]}",
    )
    await db_session.flush()
    connector_id = result.connector.id
    await db_session.execute(
        update(APIKey)
        .where(APIKey.connector_id == connector_id)
        .values(key_hash="0" * 64)
        .execution_options(synchronize_session="fetch")
    )

    with pytest.raises(ValidationError, match="matches no active API key"):
        await service.rotate_kmc_key(
            workspace_id=workspace.id, connector_id=connector_id, user_id=user_id
        )
    count = (
        await db_session.execute(
            select(func.count(APIKey.id)).where(APIKey.connector_id == connector_id)
        )
    ).scalar_one()
    assert count == 1


async def _provision_with_key(db: AsyncSession, workspace: Workspace, user_id: str):
    result = await ConnectorProvisioningService(db).provision_connector(
        workspace_id=workspace.id,
        user_id=user_id,
        connector_type="slack",
        resource_id=f"slack_{uuid4().hex[:8]}",
        auto_create_context_name=f"slack-{uuid4().hex[:8]}",
    )
    await db.flush()
    return result.connector


async def _connector_key_count(db: AsyncSession, workspace_id) -> int:
    from models.auth import APIKey

    return (
        await db.execute(select(func.count(APIKey.id)).where(APIKey.workspace_id == workspace_id))
    ).scalar_one()


@pytest.mark.asyncio
async def test_rotate_kmc_key_does_not_heal_a_revoked_key(db_session: AsyncSession):
    """#2305 review: a stored key whose row was revoked (someone revoked the
    credential) is not "gone" — no new key is minted, the 422 stays."""
    from sqlalchemy import update

    from models.auth import APIKey

    user_id, workspace = await _seed_workspace(db_session, plan_name="promax")
    connector = await _provision_with_key(db_session, workspace, user_id)
    await db_session.execute(
        update(APIKey)
        .where(APIKey.connector_id == connector.id)
        .values(revoked_at=func.now())
        .execution_options(synchronize_session="fetch")
    )

    with pytest.raises(ValidationError, match="matches no active API key"):
        await ConnectorProvisioningService(db_session).rotate_kmc_key(
            workspace_id=workspace.id, connector_id=connector.id, user_id=user_id
        )
    assert await _connector_key_count(db_session, workspace.id) == 1


@pytest.mark.asyncio
async def test_rotate_kmc_key_does_not_heal_past_an_unlinked_key_of_its_name(
    db_session: AsyncSession,
):
    """#2305 review: an active unlinked key named after the connector (a
    legacy row whose hash is not the stored key's) is drift — the 422, not
    create_key's name clash as a 500."""
    from sqlalchemy import update

    from models.auth import APIKey

    user_id, workspace = await _seed_workspace(db_session, plan_name="promax")
    connector = await _provision_with_key(db_session, workspace, user_id)
    await db_session.execute(
        update(APIKey)
        .where(APIKey.connector_id == connector.id)
        .values(connector_id=None, key_hash="1" * 64)
        .execution_options(synchronize_session="fetch")
    )

    with pytest.raises(ValidationError, match="matches no active API key"):
        await ConnectorProvisioningService(db_session).rotate_kmc_key(
            workspace_id=workspace.id, connector_id=connector.id, user_id=user_id
        )
    assert await _connector_key_count(db_session, workspace.id) == 1


@pytest_asyncio.fixture
async def committed_connector(async_engine):
    """A committed workspace + connector with its key, for two-session tests;
    removed afterwards (the database outlives a test)."""
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as db:
        owner_id, workspace = await _seed_workspace(db, plan_name="promax")
        await _add_user(db, owner_id)
        admin_id = f"admin-{uuid4().hex}"
        db.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=admin_id, role=WorkspaceRole.ADMIN)
        )
        await db.flush()
        connector = await _provision_with_key(db, workspace, admin_id)
        await db.commit()
        yield SimpleNamespace(
            maker=maker,
            owner_id=owner_id,
            admin_id=admin_id,
            workspace_id=workspace.id,
            connector_id=connector.id,
        )
        await db.rollback()
        await db.execute(delete(Workspace).where(Workspace.id == workspace.id))
        await db.execute(delete(User).where(User.user_id == owner_id))
        await db.commit()


@pytest.mark.asyncio
async def test_rotate_kmc_key_waits_for_a_concurrent_reown(committed_connector):
    """#2305 review: a rotation racing the erasure's re-own waits on the key
    row and mints under the owner it finds after that commit, never under
    the erased admin."""
    import asyncio

    from sqlalchemy import update

    from models.auth import APIKey

    c = committed_connector
    async with c.maker() as erasure, c.maker() as rotation:
        await erasure.execute(
            update(APIKey)
            .where(APIKey.connector_id == c.connector_id)
            .values(user_id=c.owner_id)
            .execution_options(synchronize_session=False)
        )
        task = asyncio.create_task(
            ConnectorProvisioningService(rotation).rotate_kmc_key(
                workspace_id=c.workspace_id, connector_id=c.connector_id, user_id=c.admin_id
            )
        )
        await asyncio.sleep(0.5)
        assert not task.done()
        await erasure.commit()
        rotated = await asyncio.wait_for(task, timeout=10)
        await rotation.commit()

    assert rotated.key_owner_user_id == c.owner_id


@pytest.mark.asyncio
async def test_concurrent_heals_mint_one_key(committed_connector):
    """#2305 review: two heals of one connector queue on the heal lock; the
    second finds the first's key and mints nothing."""
    import asyncio

    from models.auth import APIKey

    c = committed_connector
    async with c.maker() as db:
        await db.execute(delete(APIKey).where(APIKey.connector_id == c.connector_id))
        await db.commit()

    async with c.maker() as first, c.maker() as second:
        rotated = await ConnectorProvisioningService(first).rotate_kmc_key(
            workspace_id=c.workspace_id, connector_id=c.connector_id, user_id=c.admin_id
        )
        task = asyncio.create_task(
            ConnectorProvisioningService(second).rotate_kmc_key(
                workspace_id=c.workspace_id, connector_id=c.connector_id, user_id=c.admin_id
            )
        )
        await asyncio.sleep(0.5)
        assert not task.done()
        await first.commit()
        with pytest.raises(ValidationError, match="matches no active API key"):
            await asyncio.wait_for(task, timeout=10)
        await second.rollback()

    async with c.maker() as db:
        rows = (
            (await db.execute(select(APIKey).where(APIKey.connector_id == c.connector_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].user_id == c.owner_id
    assert rotated.key_owner_user_id == c.owner_id


@pytest.mark.asyncio
async def test_rotate_kmc_key_mints_for_the_owner_when_the_key_owner_left(
    db_session: AsyncSession,
):
    """#2305 review: the key's owner is no longer a member (removed, or an
    erased subject a racing rotation minted under) — the old key is revoked
    and the replacement goes to the workspace's owner, not the non-member."""
    from models.auth import APIKey

    owner_id, workspace = await _seed_workspace(db_session, plan_name="promax")
    await _add_user(db_session, owner_id)
    admin_id = f"admin-{uuid4().hex}"
    db_session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=admin_id, role=WorkspaceRole.ADMIN)
    )
    await db_session.flush()
    connector = await _provision_with_key(db_session, workspace, admin_id)
    await db_session.execute(
        delete(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id, WorkspaceMember.user_id == admin_id
        )
    )
    await db_session.flush()

    with patch("services.connector_provisioning.logger") as logger:
        rotated = await ConnectorProvisioningService(db_session).rotate_kmc_key(
            workspace_id=workspace.id, connector_id=connector.id, user_id=owner_id
        )
    await db_session.flush()

    assert rotated.key_owner_user_id == owner_id
    rows = (
        (
            await db_session.execute(
                select(APIKey).where(APIKey.connector_id == connector.id).order_by(APIKey.id)
            )
        )
        .scalars()
        .all()
    )
    old, new = rows
    assert old.user_id == admin_id and old.revoked_at is not None
    assert new.user_id == owner_id and new.revoked_at is None
    assert logger.info.call_args.args[0] == "connector_kmc_key_reowned_on_rotate"


@pytest.mark.asyncio
async def test_rotate_kmc_key_keeps_a_members_key_with_them(db_session: AsyncSession):
    """#2305 review: a key owner who is still a member keeps the replacement
    (the #1752 contract), whoever rotates."""
    owner_id, workspace = await _seed_workspace(db_session, plan_name="promax")
    admin_id = f"admin-{uuid4().hex}"
    db_session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=admin_id, role=WorkspaceRole.ADMIN)
    )
    await db_session.flush()
    connector = await _provision_with_key(db_session, workspace, admin_id)

    rotated = await ConnectorProvisioningService(db_session).rotate_kmc_key(
        workspace_id=workspace.id, connector_id=connector.id, user_id=owner_id
    )

    assert rotated.key_owner_user_id == admin_id


@pytest.mark.asyncio
async def test_rotate_kmc_key_heal_refuses_an_owner_without_a_user_row(
    db_session: AsyncSession,
):
    """#2305 review: the owner's ``users`` row is share-locked before the
    mint; a row gone (the owner's erasure committed while the heal waited) is
    a retriable refusal, never a key under the erased owner."""
    from models.auth import APIKey

    user_id, workspace = await _seed_workspace(db_session, plan_name="promax")
    connector = await _provision_with_key(db_session, workspace, user_id)
    await db_session.execute(delete(APIKey).where(APIKey.connector_id == connector.id))

    with pytest.raises(ValidationError, match="being erased"):
        await ConnectorProvisioningService(db_session).rotate_kmc_key(
            workspace_id=workspace.id, connector_id=connector.id, user_id=user_id
        )
    assert await _connector_key_count(db_session, workspace.id) == 0


@pytest.mark.asyncio
async def test_rotate_kmc_key_heal_lock_times_out_as_a_retriable_503(committed_connector):
    """#2305 review: the heal's advisory lock waits at most 5 s (the seat-lock
    pattern) and answers a retriable 503 ``CONNECTOR-003``."""
    from sqlalchemy import text

    from models.auth import APIKey
    from utils.exceptions import MemoryCloudException

    c = committed_connector
    async with c.maker() as db:
        await db.execute(delete(APIKey).where(APIKey.connector_id == c.connector_id))
        await db.commit()

    async with c.maker() as holder, c.maker() as rotation:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"connector_kmc_key_heal:{c.connector_id}"},
        )
        with pytest.raises(MemoryCloudException) as caught:
            await ConnectorProvisioningService(rotation).rotate_kmc_key(
                workspace_id=c.workspace_id, connector_id=c.connector_id, user_id=c.admin_id
            )
        await holder.rollback()

    assert caught.value.status_code == 503
    assert caught.value.error_code == "CONNECTOR-003"


@pytest.mark.asyncio
async def test_admin_non_owner_can_auto_create_context(db_session: AsyncSession):
    """Code-review fix: auto-create attributes the context to the workspace owner.

    The endpoint admits workspace ADMINS, but ContextService restricts private
    contexts to OWNER. Provisioning passes created_by=owner so an admin acting
    principal does not hit 'Only workspace owners can create private contexts'.
    """
    from models.auth import Context

    owner_id, workspace = await _seed_workspace(db_session, plan_name="promax")
    admin_id = f"admin-{uuid4().hex}"
    db_session.add(
        WorkspaceMember(
            workspace_id=workspace.id,
            user_id=admin_id,
            role=WorkspaceRole.ADMIN,
        )
    )
    await db_session.flush()

    result = await ConnectorProvisioningService(
        db_session
    ).provision_connector(
        workspace_id=workspace.id,
        user_id=admin_id,  # acting principal is a NON-owner admin
        connector_type="slack",
        resource_id=f"slack_{uuid4().hex[:8]}",
        auto_create_context_name=f"slack-{uuid4().hex[:8]}",
    )
    await db_session.flush()

    assert result.context_id is not None
    ctx = (
        await db_session.execute(select(Context).where(Context.id == result.context_id))
    ).scalar_one()
    # Context attributed to the owner, not the acting admin.
    assert ctx.created_by == owner_id
    # Issue #887: an auto-created connector context is stamped 'external' — the
    # authoritative trust signal that excludes connector-ingested memories from
    # behaviour-influencing (trust_tier='trusted') reads.
    assert ctx.trust_tier == "external"


@pytest.mark.asyncio
async def test_duplicate_team_id_is_rejected(db_session: AsyncSession):
    """One app-qualified platform team maps to exactly one connector.

    A second connector claiming the same (connector_type, app_key,
    external_team_id) is rejected with a 409 ConflictError — preventing
    cross-tenant dispatch hijack while allowing a team to install another app.
    """
    from utils.exceptions import ConflictError

    user_id, workspace = await _seed_workspace(db_session, plan_name="promax")
    svc = ConnectorProvisioningService(db_session)
    await svc.provision_connector(
        workspace_id=workspace.id,
        user_id=user_id,
        connector_type="slack",
        resource_id=f"slack_{uuid4().hex[:8]}",
        auto_create_context_name=f"slack-{uuid4().hex[:8]}",
        external_team_id="T0DUP",
    )
    await db_session.flush()

    with pytest.raises(ConflictError):
        await svc.provision_connector(
            workspace_id=workspace.id,
            user_id=user_id,
            connector_type="slack",
            resource_id=f"slack_{uuid4().hex[:8]}",
            auto_create_context_name=f"slack-{uuid4().hex[:8]}",
            external_team_id="T0DUP",  # same team — must be rejected
        )


@pytest.mark.asyncio
async def test_team_conflict_here_vs_elsewhere(db_session: AsyncSession):
    """#1753: the same team in the caller's workspace is "here" and names the
    connector; the same team held by another workspace (same app or not) is
    "elsewhere" and carries nothing about that workspace."""
    from models.worker_app import WorkerAppIdentity
    from utils.exceptions import ConflictError

    team = f"T{uuid4().hex[:10].upper()}"
    owner_a, workspace_a = await _seed_workspace(db_session)
    owner_b, workspace_b = await _seed_workspace(db_session)
    db_session.add(
        WorkerAppIdentity(
            platform="slack",
            app_key="sales",
            display_name="Sales app",
            status="active",
            active_signing_secret_encrypted="test-ciphertext",
            active_secret_revision=1,
            created_by=owner_a,
        )
    )
    await db_session.flush()
    svc = ConnectorProvisioningService(db_session)
    first = await svc.provision_connector(
        workspace_id=workspace_a.id,
        user_id=owner_a,
        connector_type="slack",
        resource_id=f"slack_{uuid4().hex[:8]}",
        display_name="Acme Slack",
        external_team_id=team,
    )
    await db_session.flush()

    async def _conflict(workspace_id, user_id, app_key):
        with pytest.raises(ConflictError) as exc:
            await svc.provision_connector(
                workspace_id=workspace_id,
                user_id=user_id,
                connector_type="slack",
                app_key=app_key,
                resource_id=f"slack_{uuid4().hex[:8]}",
                external_team_id=team,
            )
        return exc.value

    here = await _conflict(workspace_a.id, owner_a, "default")
    assert here.status_code == 409
    assert here.details == {
        "reason": "connector_team_connected_here",
        "connector_id": str(first.connector.id),
        "display_name": "Acme Slack",
    }

    for app_key in ("default", "sales"):
        elsewhere = await _conflict(workspace_b.id, owner_b, app_key)
        assert elsewhere.status_code == 409
        assert elsewhere.details == {"reason": "connector_team_connected_elsewhere"}
        text = f"{elsewhere.message} {elsewhere.details}"
        for leaked in (team, str(first.connector.id), str(workspace_a.id), owner_a):
            assert leaked not in text
        assert "Acme" not in text

    # Same workspace, different app: still allowed (one tenant, several apps).
    await svc.provision_connector(
        workspace_id=workspace_a.id,
        user_id=owner_a,
        connector_type="slack",
        app_key="sales",
        resource_id=f"slack_{uuid4().hex[:8]}",
        external_team_id=team,
    )


@pytest.mark.asyncio
async def test_refresh_slack_bot_token_is_workspace_and_slack_scoped(db_session: AsyncSession):
    """#1880: a refused re-install's bot token replaces the stored one on the
    caller's own Slack connector only, and bumps ``config_version`` when (and
    only when) the token changed."""
    owner_a, workspace_a = await _seed_workspace(db_session)
    _owner_b, workspace_b = await _seed_workspace(db_session)
    svc = ConnectorProvisioningService(db_session)
    slack = await svc.provision_connector(
        workspace_id=workspace_a.id,
        user_id=owner_a,
        connector_type="slack",
        resource_id=f"slack_{uuid4().hex[:8]}",
        oauth_tokens={"bot_token": "xoxb-old", "installing_admin_user_id": "U1"},
        external_team_id=f"T{uuid4().hex[:10].upper()}",
    )
    discord = await svc.provision_connector(
        workspace_id=workspace_a.id,
        user_id=owner_a,
        connector_type="discord",
        resource_id=f"discord_{uuid4().hex[:8]}",
        oauth_tokens={"bot_token": "discord-old"},
    )
    await db_session.flush()
    version = slack.connector.config_version

    # Another workspace, and another platform's connector: nothing is written.
    assert not await svc.refresh_slack_bot_token(workspace_b.id, slack.connector.id, "xoxb-x")
    assert not await svc.refresh_slack_bot_token(workspace_a.id, discord.connector.id, "xoxb-x")
    await db_session.refresh(slack.connector)
    await db_session.refresh(discord.connector)
    assert slack.connector.get_oauth_tokens()["bot_token"] == "xoxb-old"
    assert discord.connector.get_oauth_tokens()["bot_token"] == "discord-old"
    assert slack.connector.config_version == version

    assert await svc.refresh_slack_bot_token(workspace_a.id, slack.connector.id, "xoxb-new")
    await db_session.flush()
    await db_session.refresh(slack.connector)
    assert slack.connector.get_oauth_tokens() == {
        "bot_token": "xoxb-new",
        "installing_admin_user_id": "U1",
    }
    assert slack.connector.config_version == version + 1

    # The same token again (a re-consent that only widened the grant).
    assert await svc.refresh_slack_bot_token(workspace_a.id, slack.connector.id, "xoxb-new")
    await db_session.refresh(slack.connector)
    assert slack.connector.config_version == version + 1


@pytest.mark.asyncio
async def test_same_team_id_is_allowed_under_two_app_identities(db_session: AsyncSession):
    """The dispatch uniqueness boundary is (platform, app_key, team_id)."""
    from models.worker_app import WorkerAppIdentity

    user_id, workspace = await _seed_workspace(db_session, plan_name="promax")
    custom_app = WorkerAppIdentity(
        platform="slack",
        app_key="sales",
        display_name="Sales app",
        status="active",
        active_signing_secret_encrypted="test-ciphertext",
        active_secret_revision=1,
        created_by=user_id,
    )
    db_session.add(custom_app)
    await db_session.flush()

    svc = ConnectorProvisioningService(db_session)
    await svc.provision_connector(
        workspace_id=workspace.id,
        user_id=user_id,
        connector_type="slack",
        app_key="default",
        resource_id=f"slack_{uuid4().hex[:8]}",
        external_team_id="T0SHARED",
    )
    await svc.provision_connector(
        workspace_id=workspace.id,
        user_id=user_id,
        connector_type="slack",
        app_key="sales",
        resource_id=f"slack_{uuid4().hex[:8]}",
        external_team_id="T0SHARED",
    )
    await db_session.flush()

    assert (
        await svc.get_connector_for_dispatch("slack", "T0SHARED", app_key="default")
    ).app_key == "default"
    assert (
        await svc.get_connector_for_dispatch("slack", "T0SHARED", app_key="sales")
    ).app_key == "sales"


@pytest.mark.asyncio
async def test_provision_rejects_explicitly_disabled_default_identity(db_session: AsyncSession):
    """The migration-window exemption for app_key='default' covers the
    ABSENT/unconfigured states only. An explicitly disabled default is an
    admin revocation: binding a new connector to it would create a connector
    whose dispatch immediately 410s, so provisioning must fail closed."""
    from models.worker_app import WorkerAppIdentity
    from services.worker_app_identity import WorkerAppIdentityService
    from utils.exceptions import ValidationError

    user_id, workspace = await _seed_workspace(db_session, plan_name="promax")
    # e68_1315 seeds slack/default, so disable the existing row rather than
    # inserting a duplicate; the session rollback restores it afterwards.
    seeded = await WorkerAppIdentityService(db_session).get_identity("slack", "default")
    if seeded is None:
        db_session.add(
            WorkerAppIdentity(
                platform="slack",
                app_key="default",
                display_name="Default",
                status="disabled",
                created_by=user_id,
            )
        )
    else:
        seeded.status = "disabled"
    await db_session.flush()

    svc = ConnectorProvisioningService(db_session)
    with pytest.raises(ValidationError):
        await svc.provision_connector(
            workspace_id=workspace.id,
            user_id=user_id,
            connector_type="slack",
            app_key="default",
            resource_id=f"slack_{uuid4().hex[:8]}",
            external_team_id="T0DISABLED",
        )


@pytest.mark.asyncio
async def test_orphan_context_cleaned_up_on_post_context_failure(db_session: AsyncSession):
    """Self-review fix: a failure AFTER the context is committed must not orphan it.

    The context is committed by ContextService.create_context() before the
    connector/key/token are persisted; a token-mint failure must trigger the
    orphan-context cleanup so no context is left dangling.
    """
    from models.auth import Context

    user_id, workspace = await _seed_workspace(db_session, plan_name="promax")
    ctx_name = f"slack-{uuid4().hex[:8]}"

    with patch(
        "services.connector_provisioning.ResourceTokenManager.create_token",
        new=AsyncMock(side_effect=RuntimeError("token mint failed")),
    ):
        with pytest.raises(RuntimeError, match="token mint failed"):
            await ConnectorProvisioningService(db_session).provision_connector(
                workspace_id=workspace.id,
                user_id=user_id,
                connector_type="slack",
                resource_id=f"slack_{uuid4().hex[:8]}",
                auto_create_context_name=ctx_name,
            )

    # The auto-created context must have been cleaned up (not orphaned).
    orphan = (
        await db_session.execute(select(func.count(Context.id)).where(Context.name == ctx_name))
    ).scalar_one()
    assert orphan == 0, "auto-created context was orphaned after token-mint failure"
