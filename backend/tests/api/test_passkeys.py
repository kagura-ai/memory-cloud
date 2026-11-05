"""Software authenticator and storage tests for the public passkey routes (#2645)."""

import hashlib
import json
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import BackgroundTasks, HTTPException, Request, Response
from sqlalchemy import select
from webauthn.helpers import bytes_to_base64url

from api.routes import passkeys as pk
from models.auth import AuditLog, User, WebAuthnCredential
from services.user_deletion_service import delete_user_rows


class Authenticator:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = uuid4().bytes
        self.rp_hash = hashlib.sha256(b"localhost").digest()

    def response(
        self,
        challenge,
        handle,
        *,
        registering=False,
        origin="http://localhost:3000",
        count=1,
        uv=True,
        cross_origin=False,
    ):
        data = json.dumps(
            {
                "type": "webauthn.create" if registering else "webauthn.get",
                "challenge": challenge,
                "origin": origin,
                "crossOrigin": cross_origin,
            }
        ).encode()
        flags = 1 | (4 if uv else 0) | (0x40 if registering else 0)
        authdata = self.rp_hash + bytes([flags]) + struct.pack(">I", count)
        response = {"clientDataJSON": bytes_to_base64url(data)}
        if registering:
            x, y = (
                self.key.public_key().public_numbers().x,
                self.key.public_key().public_numbers().y,
            )
            cose_key = cbor2.dumps(
                {1: 2, 3: -7, -1: 1, -2: x.to_bytes(32, "big"), -3: y.to_bytes(32, "big")}
            )
            authdata += (
                bytes(16)
                + struct.pack(">H", len(self.credential_id))
                + self.credential_id
                + cose_key
            )
            response.update(
                attestationObject=bytes_to_base64url(
                    cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": authdata})
                ),
                transports=["internal"],
            )
        else:
            signature = self.key.sign(
                authdata + hashlib.sha256(data).digest(), ec.ECDSA(hashes.SHA256())
            )
            response.update(
                authenticatorData=bytes_to_base64url(authdata),
                signature=bytes_to_base64url(signature),
                userHandle=bytes_to_base64url(handle),
            )
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "response": response,
        }


def request(cookie=None):
    value = f"{pk.SESSION_COOKIE_NAME}=browser"
    if cookie:
        value += f"; {pk._BROWSER_COOKIE}={cookie}"
    return Request(
        {"type": "http", "headers": [(b"cookie", value.encode())], "client": ("127.0.0.1", 80)}
    )


@pytest.fixture
def ceremony(monkeypatch):
    values = {}

    async def setex(key, ttl, value):
        assert 0 < ttl <= 300
        values[key] = value

    async def take(key):
        return values.pop(key, None)

    async def get(key):
        return values.get(key)

    monkeypatch.setattr(
        pk, "get_redis_client", lambda: SimpleNamespace(setex=setex, getdel=take, get=get)
    )
    monkeypatch.setattr(
        pk,
        "get_settings",
        lambda: SimpleNamespace(
            frontend_url="http://localhost:3000",
            webauthn_rp_id=None,
            webauthn_origins=None,
            webauthn_rp_name="Test",
        ),
    )
    monkeypatch.setattr(pk, "increment_counter", AsyncMock(return_value=1))
    monkeypatch.setattr(pk.auth, "_session_manager", Mock())
    monkeypatch.setattr(pk.auth, "_reserve_login_attempt", Mock(return_value=1))
    monkeypatch.setattr(pk.auth, "_clear_login_failures", Mock())
    monkeypatch.setattr(pk.auth, "_create_password_session", AsyncMock(return_value="new-session"))
    monkeypatch.setattr(pk.auth, "_complete_password_sign_in", AsyncMock(return_value=True))
    monkeypatch.setattr(pk.auth, "_record_terms_acceptance", AsyncMock())
    monkeypatch.setattr(pk.auth, "_set_session_cookie", Mock())
    monkeypatch.setattr(pk, "note_browser_sign_in", AsyncMock())
    return Authenticator()


@pytest.mark.asyncio
async def test_bound_challenge_cannot_cross_accounts_or_replay(ceremony):
    options = await pk._issue(pk.generate_authentication_options(rp_id="localhost"), "first")
    with pytest.raises(HTTPException, match="expired"):
        await pk._consume(options["challenge"], "second")
    assert await pk._consume(options["challenge"], "first")
    with pytest.raises(HTTPException, match="expired"):
        await pk._consume(options["challenge"], "first")


@pytest.mark.asyncio
async def test_overlapping_options_keep_both_challenges_usable(ceremony):
    first = Response()
    options_a = await pk.authenticate_options(request(), first)
    binding = first.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    second = Response()
    options_b = await pk.authenticate_options(request(binding), second)
    assert second.headers["set-cookie"].startswith(f"{pk._BROWSER_COOKIE}={binding};")
    assert pk._first_cookie(options_a["challenge"]) in first.headers.getlist("set-cookie")[1]
    assert options_a["challenge"] != options_b["challenge"]
    assert await pk._consume(options_a["challenge"], binding)
    assert await pk._consume(options_b["challenge"], binding)


@pytest.mark.asyncio
async def test_register_cap_authenticate_remove_and_erasure(db_session, ceremony, monkeypatch):
    db = db_session
    owner = User(user_id=uuid4().hex, email=f"{uuid4().hex}@example.org", auth_provider="google")
    db.add(owner)
    await db.commit()
    session = {"user_id": owner.user_id}
    authn = ceremony
    handle = pk._handle(owner)
    tasks = BackgroundTasks()
    req = request()

    async def register():
        options = await pk.register_options(pk.Proof(), req, session, db)
        assert options["authenticatorSelection"]["residentKey"] == "required"
        assert options["authenticatorSelection"]["userVerification"] == "required"
        response = authn.response(options["challenge"], handle, registering=True)
        return await pk.register_verify(
            pk.CredentialResponse(challenge=options["challenge"], credential=response),
            req,
            session,
            tasks,
            db,
        )

    pk.auth._session_manager.proven_within.return_value = False
    with pytest.raises(HTTPException, match="Verify"):
        await register()
    pk.auth._session_manager.proven_within.return_value = True
    pending = await pk.register_options(pk.Proof(), req, session, db)
    owner.mfa_generation = uuid4().hex
    await db.commit()
    stale = pk.CredentialResponse(
        challenge=pending["challenge"],
        credential=authn.response(pending["challenge"], handle, registering=True),
    )
    with pytest.raises(HTTPException, match="expired"):
        await pk.register_verify(stale, req, session, tasks, db)
    key = await register()
    monkeypatch.setattr(pk, "_LIMIT", 1)
    with pytest.raises(HTTPException, match="limit"):
        await register()

    async def authenticate(**kwargs):
        response = Response()
        options = await pk.authenticate_options(req, response)
        binding = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        body = pk.SignIn(
            challenge=options["challenge"],
            credential=authn.response(options["challenge"], handle, **kwargs),
            accepted_terms="current",
        )
        return await pk.authenticate_verify(body, request(binding), Response(), "/profile", db)

    await authenticate(count=2)
    assert key.sign_count == 2 and key.last_used_at
    assert pk.auth._create_password_session.await_count == 1
    for bad in [
        {"count": 1},
        {"count": 3, "origin": "https://other.example"},
        {"count": 3, "uv": False},
        {"count": 3, "cross_origin": True},
    ]:
        with pytest.raises(HTTPException):
            await authenticate(**bad)
    assert pk.auth._create_password_session.await_count == 1

    with pytest.raises(HTTPException, match="only remaining"):
        await pk.remove_passkey(key.id, pk.Proof(), req, session, tasks, db)
    owner.password_hash = pk.auth.hash_password("test password")
    await db.commit()
    with pytest.raises(HTTPException, match="Verify"):
        await pk.remove_passkey(
            key.id, pk.Proof(current_password="incorrect"), req, session, tasks, db
        )
    await pk.rename_passkey(key.id, pk.Rename(name="Hello"), session, db)
    assert (await pk.list_passkeys(session, db))[0].name == "Hello"
    await pk.remove_passkey(
        key.id, pk.Proof(current_password="test password"), req, session, tasks, db
    )
    with pytest.raises(HTTPException):
        await authenticate(count=4)
    assert {
        row.action
        for row in await db.scalars(select(AuditLog).where(AuditLog.user_id == owner.user_id))
    } == {"passkey_added", "passkey_removed"}
    again = await register()
    key_id = again.id
    await delete_user_rows(db, owner.user_id)
    await db.commit()
    assert (
        await db.scalar(select(WebAuthnCredential.id).where(WebAuthnCredential.id == key_id))
        is None
    )
