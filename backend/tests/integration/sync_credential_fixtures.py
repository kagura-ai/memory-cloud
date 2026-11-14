"""Real keys and fresh transaction sessions on the workspace-owned installation."""

from datetime import UTC
from types import SimpleNamespace
from uuid import uuid4

import pytest_asyncio
from joserfc import jwt
from joserfc.jwk import RSAKey

from auth.sync_service_jwt import ASSERTION_TYPE, key_fingerprint, public_key
from config.settings import get_settings
from models.sync_credential import SyncCredentialClient, SyncSigningKey
from models.sync_installation import SyncService
from services import sync_credentials
from tests.integration import sync_installation_fixtures as installations

store = installations.store
ISSUER = "https://identity.example.test/oidc"


@pytest_asyncio.fixture(loop_scope="session")
async def worker(store, monkeypatch):
    approval = await installations.approve(store)
    await installations.paid(store)
    result = await installations.http(
        store, "POST", "", billing=True, body=installations.activation(approval)
    )
    assert result.status_code == 200, result.text
    target = result.json()
    monkeypatch.setattr(get_settings(), "sync_credentials_enabled", True)
    monkeypatch.setattr(get_settings(), "oidc_issuer", ISSUER)
    monkeypatch.setattr(sync_credentials, "utcnow", lambda: store.clock.now)
    key = RSAKey.generate_key(2048, parameters={"kid": "current", "alg": "RS256"})
    document = public_key(key.as_dict(private=False))
    async with store.factory() as db:
        service = await db.get(SyncService, store.service_id)
        client_id = service.service_client_id
        db.add(SyncCredentialClient(client_id=client_id, service_id=store.service_id))
        await db.flush()
        signing = SyncSigningKey(
            client_id=client_id,
            kid="current",
            public_jwk=document,
            fingerprint=key_fingerprint(document),
        )
        db.add(signing)
        await db.commit()
        key_id = signing.id
    return SimpleNamespace(
        store=store,
        factory=store.factory,
        target=target,
        key=key,
        key_id=key_id,
        client_id=client_id,
    )


def token_form(worker, *, jti=None, key=None, kid="current", **changes):
    now = int(worker.store.clock.now.replace(tzinfo=UTC).timestamp())
    assertion = jwt.encode(
        {"alg": "RS256", "kid": kid, "typ": "client-authentication+jwt"},
        {
            "iss": worker.client_id,
            "sub": worker.client_id,
            "aud": ISSUER,
            "iat": now,
            "exp": now + 60,
            "jti": jti or uuid4().hex,
        },
        key or worker.key,
        default_type=None,
    )
    return {
        "grant_type": "client_credentials",
        "client_id": worker.client_id,
        "client_assertion_type": ASSERTION_TYPE,
        "client_assertion": assertion,
        "scope": "resource.sync",
        "resource": ISSUER + "/sync/installations/" + worker.target["installation_id"],
        **changes,
    }


async def mint(worker, **changes):
    async with worker.factory() as db:
        return await sync_credentials.issue_token(db, token_form(worker, **changes))
