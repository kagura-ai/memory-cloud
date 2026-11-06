"""Software authenticator and storage tests for the public passkey routes (#2645)."""

import asyncio
import hashlib
import json
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import BackgroundTasks, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from webauthn.helpers import bytes_to_base64url

from api.routes import passkeys as pk
from models.auth import AuditLog, User, UserOAuthProvider, WebAuthnCredential
from services.account_linking_service import AccountLinkingService
from services.password_account_service import PasswordAccountService
from services.user_deletion_service import delete_user_rows
from utils.exceptions import MemoryCloudException, RateLimitError, RedisError, ValidationError


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

    async def delete(*keys):
        return sum(values.pop(key, None) is not None for key in keys)

    monkeypatch.setattr(
        pk,
        "get_redis_client",
        lambda: SimpleNamespace(setex=setex, getdel=take, get=get, delete=delete, values=values),
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
    with pytest.raises(MemoryCloudException, match="expired"):
        await pk._consume(options["challenge"], "second")
    assert await pk._consume(options["challenge"], "first")
    with pytest.raises(MemoryCloudException, match="expired"):
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
    with pytest.raises(MemoryCloudException, match="Verify"):
        await register()
    pk.auth._session_manager.proven_within.return_value = True
    pending = await pk.register_options(pk.Proof(), req, session, db)
    owner.mfa_generation = uuid4().hex
    await db.commit()
    stale = pk.CredentialResponse(
        challenge=pending["challenge"],
        credential=authn.response(pending["challenge"], handle, registering=True),
    )
    with pytest.raises(MemoryCloudException, match="expired"):
        await pk.register_verify(stale, req, session, tasks, db)
    key = await register()
    monkeypatch.setattr(pk, "_LIMIT", 1)
    with pytest.raises(MemoryCloudException, match="limit"):
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
        with pytest.raises(MemoryCloudException):
            await authenticate(**bad)
    assert pk.auth._create_password_session.await_count == 1

    with pytest.raises(MemoryCloudException, match="only remaining"):
        await pk.remove_passkey(key.id, pk.Proof(), req, session, tasks, db)
    owner.password_hash = pk.auth.hash_password("test password")
    await db.commit()
    with pytest.raises(MemoryCloudException, match="Verify"):
        await pk.remove_passkey(
            key.id, pk.Proof(current_password="incorrect"), req, session, tasks, db
        )
    await pk.rename_passkey(key.id, pk.Rename(name="Hello"), session, db)
    assert (await pk.list_passkeys(session, db))[0].name == "Hello"
    await pk.remove_passkey(
        key.id, pk.Proof(current_password="test password"), req, session, tasks, db
    )
    with pytest.raises(MemoryCloudException):
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


async def pending_registration(db, account, device, proof=None):
    options = await pk.register_options(
        proof or pk.Proof(), request(), {"user_id": account.user_id}, db
    )
    await db.commit()  # the request-scoped dependency ends the options transaction
    return pk.CredentialResponse(
        challenge=options["challenge"],
        credential=device.response(options["challenge"], pk._handle(account), registering=True),
    )


@pytest.mark.asyncio
async def test_two_sessions_racing_for_last_slot_and_cross_account_duplicate(
    db_session, async_engine, ceremony, monkeypatch
):
    accounts = [User(user_id=uuid4().hex, email=f"{uuid4().hex}@example.org") for _ in range(2)]
    db_session.add_all(accounts)
    await db_session.commit()
    owner, other = accounts
    monkeypatch.setattr(pk, "_LIMIT", 1)
    requests = [
        await pending_registration(db_session, owner, device)
        for device in (ceremony, Authenticator())
    ]
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    barrier = asyncio.Barrier(2)

    async def submit(body):
        async with sessions() as db:
            await barrier.wait()
            try:
                return await pk.register_verify(
                    body, request(), {"user_id": owner.user_id}, BackgroundTasks(), db
                )
            except MemoryCloudException as exc:
                assert exc.status_code in (401, 409)
                return None

    results = await asyncio.gather(*(submit(body) for body in requests))
    assert sum(key is not None for key in results) == 1
    stored = (await pk._keys(db_session, owner.user_id))[0]

    # A malicious second account can attest a key with the same opaque ID,
    # but must never rebind the winning key, even to a different public key.
    duplicate = Authenticator()
    duplicate.credential_id = stored.credential_id
    body = await pending_registration(db_session, other, duplicate)
    with pytest.raises(MemoryCloudException, match="already registered") as denied:
        await pk.register_verify(
            body, request(), {"user_id": other.user_id}, BackgroundTasks(), db_session
        )
    assert denied.value.status_code == 409
    # A failed insert rolled the session back and expired its ORM identities.
    bindings = list(
        await db_session.scalars(
            select(WebAuthnCredential).where(
                WebAuthnCredential.credential_id == duplicate.credential_id
            )
        )
    )
    winner = next(key for key in results if key is not None)
    assert [key.id for key in bindings] == [winner.id]
    assert bindings[0].public_key == winner.public_key


@pytest.mark.asyncio
async def test_different_key_proves_removal_and_remaining_key_allows_unlink(db_session, ceremony):
    owner = User(user_id=uuid4().hex, email=f"{uuid4().hex}@example.org", auth_provider="google")
    db_session.add(owner)
    await db_session.commit()
    user = {"user_id": owner.user_id}
    tasks = BackgroundTasks()
    req = request()
    handle = pk._handle(owner)
    body = await pending_registration(db_session, owner, ceremony)
    first = await pk.register_verify(body, req, user, tasks, db_session)
    options = await pk.proof_options(req, user, None, db_session)
    proof = pk.Proof(
        challenge=options["challenge"],
        assertion=ceremony.response(options["challenge"], handle, count=2),
    )
    second_device = Authenticator()
    body = await pending_registration(db_session, owner, second_device, proof)
    second = await pk.register_verify(body, req, user, tasks, db_session)
    assert second.id != first.id

    async def removal_proof(device):
        options = await pk.proof_options(req, user, second.id, db_session)
        assert [item["id"] for item in options["allowCredentials"]] == [
            bytes_to_base64url(first.credential_id)
        ]
        return pk.Proof(
            challenge=options["challenge"],
            assertion=device.response(options["challenge"], handle, count=3),
        )

    with pytest.raises(MemoryCloudException, match="Invalid passkey"):
        await pk.remove_passkey(
            second.id, await removal_proof(second_device), req, user, tasks, db_session
        )
    await pk.remove_passkey(second.id, await removal_proof(ceremony), req, user, tasks, db_session)
    assert [key.id for key in await pk._keys(db_session, owner.user_id)] == [first.id]

    # All three method-removal entry points use the same locked owner rule.
    linking = AccountLinkingService(db_session)
    await linking.link(
        user_id=owner.user_id, provider="google", oauth_sub=uuid4().hex, email=owner.email
    )
    await linking.unlink(user_id=owner.user_id, provider="google")
    owner.password_hash = pk.auth.hash_password("current")
    await db_session.commit()
    await PasswordAccountService(db_session).remove(
        user_id=owner.user_id, current_password="current"
    )
    with pytest.raises(MemoryCloudException, match="only remaining"):
        await pk.remove_passkey(first.id, pk.Proof(), req, user, tasks, db_session)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["github", "google"])
async def test_oauth_only_removal_requires_mailbox_password(db_session, ceremony, provider):
    """A recent session may have been proved with the very key being removed."""
    owner = User(user_id=uuid4().hex, email=f"{uuid4().hex}@example.org", auth_provider=provider)
    db_session.add(owner)
    await db_session.commit()
    await AccountLinkingService(db_session).link(
        user_id=owner.user_id, provider=provider, oauth_sub=uuid4().hex, email=owner.email
    )
    user = {"user_id": owner.user_id}
    req, tasks = request(), BackgroundTasks()
    pk.auth._session_manager.proven_within.return_value = True
    body = await pending_registration(db_session, owner, ceremony)
    key = await pk.register_verify(body, req, user, tasks, db_session)
    with pytest.raises(MemoryCloudException, match="Set up a password") as denied:
        await pk.remove_passkey(key.id, pk.Proof(), req, user, tasks, db_session)
    assert denied.value.status_code == 403
    assert await pk._keys(db_session, owner.user_id) == [key]

    email = Mock(send_password_setup=AsyncMock(return_value=True))
    passwords = PasswordAccountService(db_session, email_service=email)
    await passwords.request_setup(user_id=owner.user_id)
    delivery = email.send_password_setup.await_args.kwargs
    assert delivery["to_email"] == owner.email
    token = parse_qs(urlsplit(delivery["setup_url"]).query)["token"][0]
    new_password = "New-Mailbox-Password-123!"
    await passwords.complete_setup(raw_token=token, new_password=new_password)
    with pytest.raises(MemoryCloudException) as incorrect:
        await pk.remove_passkey(
            key.id, pk.Proof(current_password="wrong"), req, user, tasks, db_session
        )
    assert incorrect.value.status_code == 403
    result = await pk.remove_passkey(
        key.id, pk.Proof(current_password=new_password), req, user, tasks, db_session
    )
    assert result.status_code == 204
    assert not await pk._keys(db_session, owner.user_id)
    assert (
        await db_session.scalar(
            select(UserOAuthProvider.provider).where(UserOAuthProvider.user_id == owner.user_id)
        )
        == provider
    )


@pytest.fixture
def attempts(ceremony, monkeypatch):
    """A counting INCR on the ceremony store that records each key and TTL."""
    store = pk.get_redis_client()
    calls = []

    async def incr(key, ttl=None):
        calls.append((key, ttl))
        store.values[key] = store.values.get(key, 0) + 1
        return store.values[key]

    monkeypatch.setattr(pk, "increment_counter", incr)
    return SimpleNamespace(values=store.values, calls=calls)


async def enrolled(db, device):
    """An account with one registered passkey (sign count 1)."""
    owner = User(user_id=uuid4().hex, email=f"{uuid4().hex}@example.org", auth_provider="google")
    db.add(owner)
    await db.commit()
    pk.auth._session_manager.proven_within.return_value = True
    body = await pending_registration(db, owner, device)
    await pk.register_verify(body, request(), {"user_id": owner.user_id}, BackgroundTasks(), db)
    return owner


async def sign_in(db, account, device, **kwargs):
    response = Response()
    options = await pk.authenticate_options(request(), response)
    binding = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    body = pk.SignIn(
        challenge=options["challenge"],
        credential=device.response(options["challenge"], pk._handle(account), **kwargs),
    )
    return await pk.authenticate_verify(body, request(binding), Response(), "/profile", db)


@pytest.mark.asyncio
async def test_passkey_sign_in_draws_on_its_own_counter(db_session, ceremony, attempts):
    owner = await enrolled(db_session, ceremony)
    counter = f"webauthn_signin:{owner.user_id}"
    # Password and TOTP failures have spent the account's sign-in budget.
    pk.auth._reserve_login_attempt.side_effect = HTTPException(status_code=429)

    result = await sign_in(db_session, owner, ceremony, count=2)

    assert result["redirect_url"]
    assert pk.auth._create_password_session.await_count == 1
    pk.auth._reserve_login_attempt.assert_not_called()
    assert [call for call in attempts.calls if call[0] == counter] == [(counter, 300)]
    assert counter not in attempts.values
    pk.auth._clear_login_failures.assert_called_once_with(f"user:{owner.user_id}")


@pytest.mark.asyncio
async def test_failed_assertions_spend_the_passkey_counter_and_then_answer_429(
    db_session, ceremony, attempts, monkeypatch
):
    owner = await enrolled(db_session, ceremony)
    counter = f"webauthn_signin:{owner.user_id}"
    verify = Mock(wraps=pk.verify_authentication_response)
    monkeypatch.setattr(pk, "verify_authentication_response", verify)

    for _ in range(5):
        with pytest.raises(MemoryCloudException, match="Invalid passkey") as denied:
            await sign_in(db_session, owner, ceremony, count=1)
        assert denied.value.status_code == 401
    assert attempts.values[counter] == 5

    # The sixth assertion is refused before it is checked, even a valid one.
    with pytest.raises(RateLimitError) as refused:
        await sign_in(db_session, owner, ceremony, count=2)
    assert refused.value.status_code == 429
    assert refused.value.retry_after == 300
    assert verify.call_count == 5
    assert pk.auth._create_password_session.await_count == 0
    pk.auth._reserve_login_attempt.assert_not_called()
    assert not [key for key in attempts.values if key.startswith("login_attempts:")]
    user_id = owner.user_id
    await db_session.rollback()
    assert await stored_sign_count(db_session, user_id) == 1


async def stored_sign_count(db, user_id):
    return await db.scalar(
        select(WebAuthnCredential.sign_count).where(WebAuthnCredential.user_id == user_id)
    )


@pytest.mark.asyncio
async def test_a_passkey_sign_in_is_refused_when_the_attempt_counter_is_unavailable(
    db_session, ceremony, monkeypatch
):
    owner = await enrolled(db_session, ceremony)
    verify = Mock(wraps=pk.verify_authentication_response)
    monkeypatch.setattr(pk, "verify_authentication_response", verify)

    async def incr(key, ttl=None):
        if key.startswith("webauthn_signin:"):
            raise RedisError("Failed to increment counter")
        return 1

    monkeypatch.setattr(pk, "increment_counter", incr)

    with pytest.raises(RedisError):
        await sign_in(db_session, owner, ceremony, count=2)

    verify.assert_not_called()
    assert pk.auth._create_password_session.await_count == 0
    pk.auth._reserve_login_attempt.assert_not_called()
    user_id = owner.user_id
    await db_session.rollback()
    assert await stored_sign_count(db_session, user_id) == 1


@pytest.mark.asyncio
async def test_a_completed_passkey_sign_in_clears_both_counters(db_session, ceremony, attempts):
    owner = await enrolled(db_session, ceremony)
    counter = f"webauthn_signin:{owner.user_id}"
    for _ in range(4):
        with pytest.raises(MemoryCloudException, match="Invalid passkey"):
            await sign_in(db_session, owner, ceremony, count=1)
    assert attempts.values[counter] == 4

    await sign_in(db_session, owner, ceremony, count=2)

    assert counter not in attempts.values
    pk.auth._clear_login_failures.assert_called_once_with(f"user:{owner.user_id}")
    # The window started over: the next failure is an ordinary 401.
    with pytest.raises(MemoryCloudException, match="Invalid passkey") as denied:
        await sign_in(db_session, owner, ceremony, count=2)
    assert denied.value.status_code == 401
    assert attempts.values[counter] == 1


@pytest.mark.asyncio
async def test_a_passkey_proof_on_the_profile_uses_the_passkey_counter(
    db_session, ceremony, attempts
):
    owner = await enrolled(db_session, ceremony)
    counter = f"webauthn_signin:{owner.user_id}"
    user = {"user_id": owner.user_id}
    pk.auth._session_manager.proven_within.return_value = False
    options = await pk.proof_options(request(), user, None, db_session)
    proof = pk.Proof(
        challenge=options["challenge"],
        assertion=ceremony.response(options["challenge"], pk._handle(owner), count=2),
    )

    created = await pk.register_options(proof, request(), user, db_session)

    assert created["challenge"]
    assert [call for call in attempts.calls if call[0] == counter] == [(counter, 300)]
    assert counter not in attempts.values
    pk.auth._clear_login_failures.assert_called_once_with(f"user:{owner.user_id}")
    pk.auth._reserve_login_attempt.assert_not_called()


@pytest.mark.asyncio
async def test_link_provider_accepts_a_passkey_bound_to_the_provider(
    db_session, ceremony, monkeypatch
):
    from api.routes import me_account

    monkeypatch.setenv("GITHUB_CLIENT_ID", "test-client")
    monkeypatch.setenv("GITHUB_REDIRECT_URI", "http://localhost:8080/api/v1/auth/github/callback")
    owner = User(user_id=uuid4().hex, email=f"{uuid4().hex}@example.org", auth_provider="google")
    db_session.add(owner)
    await db_session.commit()
    session = {"user_id": owner.user_id}
    handle = pk._handle(owner)
    body = await pending_registration(db_session, owner, ceremony)
    await pk.register_verify(body, request(), session, BackgroundTasks(), db_session)
    pk.auth._session_manager.proven_within.return_value = False
    redis = Mock()
    pk.auth._session_manager._redis = redis

    options = await pk.proof_options(request(), session, None, db_session, "github")
    link = me_account.LinkProviderRequest(
        provider="github",
        challenge=options["challenge"],
        assertion=ceremony.response(options["challenge"], handle, count=2),
    )
    result = await me_account.link_provider(link, request(), session, db_session)

    assert result.state
    assert redis.setex.call_count == 5
    intent = next(
        c for c in redis.setex.call_args_list if c.args[0] == f"oauth2_state_intent:{result.state}"
    )
    assert intent.args[2] == "link"

    # The proof is single use.
    redis.reset_mock()
    with pytest.raises(MemoryCloudException, match="expired"):
        await me_account.link_provider(link, request(), session, db_session)
    redis.setex.assert_not_called()

    # A challenge issued for adding a passkey does not start a link.
    options = await pk.proof_options(request(), session, None, db_session)
    other_purpose = me_account.LinkProviderRequest(
        provider="github",
        challenge=options["challenge"],
        assertion=ceremony.response(options["challenge"], handle, count=3),
    )
    with pytest.raises(MemoryCloudException, match="expired"):
        await me_account.link_provider(other_purpose, request(), session, db_session)
    redis.setex.assert_not_called()

    # Nor does one issued for connecting the other provider.
    options = await pk.proof_options(request(), session, None, db_session, "google")
    wrong_provider = me_account.LinkProviderRequest(
        provider="github",
        challenge=options["challenge"],
        assertion=ceremony.response(options["challenge"], handle, count=3),
    )
    with pytest.raises(MemoryCloudException, match="expired"):
        await me_account.link_provider(wrong_provider, request(), session, db_session)
    redis.setex.assert_not_called()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_proof_options_refuses_two_purposes(db_session, ceremony):
    with pytest.raises(ValidationError, match="one proof purpose"):
        await pk.proof_options(request(), {"user_id": uuid4().hex}, uuid4(), db_session, "github")
