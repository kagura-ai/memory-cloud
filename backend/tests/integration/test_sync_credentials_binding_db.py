"""Every live target component and stored authority bound must still match."""

import hashlib
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from models.auth import Context
from models.public_mcp import PublicMcpEndpoint
from models.resource import Resource
from models.sync_credential import SyncAccessToken, SyncCredentialClient
from models.sync_installation import SyncInstallation, SyncService
from services import sync_credentials as service
from tests.integration import sync_credential_fixtures as fixtures
from tests.integration.sync_credential_fixtures import mint
from tests.integration.sync_installation_fixtures import change, transfer

pytestmark = pytest.mark.asyncio(loop_scope="session")
store = fixtures.store
worker = fixtures.worker


@pytest.mark.parametrize(
    "kind",
    [
        "resource_workspace",
        "context_workspace",
        "endpoint_workspace",
        "context_hidden",
        "context_deleted",
        "endpoint_revoked",
        "installation_resource",
        "token_context",
        "token_resource",
        "token_scope",
        "token_longer_expiry",
        "client_generation",
        "service_generation",
        "operations",
    ],
)
async def test_mismatched_or_unavailable_current_binding_is_never_admitted(worker, kind):
    result = await mint(worker)
    iid = UUID(worker.target["installation_id"])
    digest = hashlib.sha256(result["access_token"].encode()).hexdigest()
    updates = {
        "resource_workspace": (
            Resource,
            UUID(worker.target["resource_pk"]),
            {"workspace_id": worker.store.other},
        ),
        "context_workspace": (
            Context,
            UUID(worker.target["context_id"]),
            {"workspace_id": worker.store.other},
        ),
        "endpoint_workspace": (
            PublicMcpEndpoint,
            UUID(worker.target["endpoint_id"]),
            {"workspace_id": worker.store.other},
        ),
        "context_hidden": (Context, UUID(worker.target["context_id"]), {"is_public": False}),
        "context_deleted": (
            Context,
            UUID(worker.target["context_id"]),
            {"deleted_at": worker.store.clock.now},
        ),
        "endpoint_revoked": (
            PublicMcpEndpoint,
            UUID(worker.target["endpoint_id"]),
            {"revoked_at": worker.store.clock.now},
        ),
        "installation_resource": (SyncInstallation, iid, {"resource_pk": uuid4()}),
        "token_context": (SyncAccessToken, digest, {"context_id": uuid4()}),
        "token_resource": (SyncAccessToken, digest, {"resource": fixtures.ISSUER + "/other"}),
        "token_longer_expiry": (
            SyncAccessToken,
            digest,
            {"expires_at": worker.store.clock.now + timedelta(hours=1)},
        ),
        "client_generation": (SyncCredentialClient, worker.client_id, {"generation": 2}),
        "service_generation": (SyncService, worker.store.service_id, {"generation": 2}),
        "operations": (SyncInstallation, iid, {"operations": ["schema:read"]}),
    }
    if kind == "token_scope":
        # The DB check rejects widening this field before an access path exists.
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            await change(worker.store, SyncAccessToken, digest, scope="memory:admin")
        return
    model, key, values = updates[kind]
    await change(worker.store, model, key, **values)
    async with worker.factory() as db:
        with pytest.raises(service.SyncCredentialError):
            async with service.sync_token_boundary(
                db, result["access_token"], iid, operation="results:read"
            ):
                pytest.fail("Changed authority admitted")


async def test_existing_workspace_owned_token_survives_owner_transfer_without_old_login(worker):
    result = await mint(worker)
    await transfer(worker.store, "next")
    async with worker.factory() as db:
        async with service.sync_token_boundary(
            db,
            result["access_token"],
            UUID(worker.target["installation_id"]),
            operation="results:read",
        ) as authority:
            assert authority.installation.workspace_id == worker.store.wid


async def test_verifiers_contain_no_plaintext_access_credential(worker):
    result = await mint(worker)
    async with worker.factory() as db:
        row = await db.scalar(select(SyncAccessToken))
        assert row.token_hash == hashlib.sha256(result["access_token"].encode()).hexdigest()
        stored = {
            column.name: getattr(row, column.name) for column in SyncAccessToken.__table__.columns
        }
        assert result["access_token"] not in repr(stored)
