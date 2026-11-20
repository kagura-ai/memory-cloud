"""Durable replay, immutable target authority and live credential revocation."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from models.sync_credential import (
    SyncAccessToken,
    SyncAssertionUse,
    SyncCredentialClient,
    SyncSigningKey,
)
from models.sync_installation import SyncInstallation, SyncService
from services import sync_credentials as service
from tests.integration import sync_credential_fixtures as fixtures
from tests.integration.sync_credential_fixtures import mint, token_form
from tests.integration.sync_installation_fixtures import change, paid
from utils.exceptions import AuthorizationError

pytestmark = pytest.mark.asyncio(loop_scope="session")
store = fixtures.store
worker = fixtures.worker


async def test_mint_binds_hash_only_token_and_the_live_installation(worker):
    result = await mint(worker)
    assert set(result) == {"access_token", "token_type", "expires_in", "scope"}
    assert result["access_token"].startswith("kmc_sync_at_")
    assert result["expires_in"] == 300 and result["scope"] == "resource.sync"
    async with worker.factory() as db:
        row = await db.scalar(select(SyncAccessToken))
        assert row.token_hash != result["access_token"]
        assert row.workspace_id == worker.store.wid
        assert row.context_id == UUID(worker.target["context_id"])
        assert row.client_id == worker.client_id
    async with worker.factory() as db:
        async with service.sync_token_boundary(
            db,
            result["access_token"],
            UUID(worker.target["installation_id"]),
            operation="results:read",
        ) as authority:
            assert authority.installation.resource_pk == UUID(worker.target["resource_pk"])


async def test_replay_is_durable_even_when_issuance_is_denied(worker):
    form = token_form(worker)
    await paid(worker.store, 2, services=False)
    async with worker.factory() as db:
        with pytest.raises(AuthorizationError):
            await service.issue_token(db, form)
    async with worker.factory() as db:
        assert await db.scalar(select(func.count()).select_from(SyncAssertionUse)) == 1
        assert await db.scalar(select(func.count()).select_from(SyncAccessToken)) == 0
    await paid(worker.store, 3)
    async with worker.factory() as db:
        with pytest.raises(service.SyncCredentialError, match="invalid_client"):
            await service.issue_token(db, form)


@pytest.mark.parametrize(
    "kind", ["service", "client", "key", "installation", "generation", "expired"]
)
async def test_current_revocation_and_expiry_reject_an_existing_token(worker, kind):
    result = await mint(worker)
    iid = UUID(worker.target["installation_id"])
    if kind == "service":
        await change(worker.store, SyncService, worker.store.service_id, enabled=False)
    elif kind == "client":
        await change(worker.store, SyncCredentialClient, worker.client_id, enabled=False)
    elif kind == "key":
        await change(worker.store, SyncSigningKey, worker.key_id, revoked_at=worker.store.clock.now)
    elif kind == "installation":
        await change(worker.store, SyncInstallation, iid, state="revoked", generation=2)
    elif kind == "generation":
        await change(worker.store, SyncInstallation, iid, generation=2)
    else:
        worker.store.clock.now += timedelta(seconds=300)
    async with worker.factory() as db:
        with pytest.raises(service.SyncCredentialError):
            async with service.sync_token_boundary(
                db, result["access_token"], iid, operation="results:read"
            ):
                pytest.fail("Revoked authority reached the consumer")


@pytest.mark.parametrize("behind, admitted", [(10, True), (30, True), (31, False)])
async def test_token_minted_on_an_instance_whose_clock_runs_ahead(worker, behind, admitted):
    """#2946: a validating instance a little behind the minting one still admits the token."""
    result = await mint(worker)
    iid = UUID(worker.target["installation_id"])
    worker.store.clock.now -= timedelta(seconds=behind)
    async with worker.factory() as db:
        if admitted:
            async with service.sync_token_boundary(
                db, result["access_token"], iid, operation="results:read"
            ) as authority:
                assert authority.token is not None
        else:
            with pytest.raises(service.SyncCredentialError):
                async with service.sync_token_boundary(
                    db, result["access_token"], iid, operation="results:read"
                ):
                    pytest.fail("A token from beyond the skew allowance was admitted")


@pytest.mark.parametrize("field", ["client_id", "client_assertion", "resource"])
async def test_nul_byte_in_the_token_form_is_a_client_error(worker, field):
    """#2946: PostgreSQL rejects a NUL in text; issuance answers 400, never an unhandled error."""
    form = token_form(worker)
    form[field] = form[field] + "\x00"
    async with worker.factory() as db:
        with pytest.raises(service.SyncCredentialError) as raised:
            await service.issue_token(db, form)
    assert raised.value.status == 400


async def test_target_and_operation_substitution_cannot_expand_token(worker):
    result = await mint(worker)
    for iid, operation in [
        (uuid4(), "results:read"),
        (UUID(worker.target["installation_id"]), "memory:admin"),
    ]:
        async with worker.factory() as db:
            with pytest.raises(service.SyncCredentialError):
                async with service.sync_token_boundary(
                    db, result["access_token"], iid, operation=operation
                ):
                    pytest.fail("Substitution was admitted")
