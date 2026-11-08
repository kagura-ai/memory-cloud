"""Discoverable, user-verified WebAuthn credentials (#2645)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    parse_authentication_credential_json,
    parse_client_data_json,
    parse_registration_credential_json,
)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    CredentialDeviceType,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from api.routes import auth
from auth.dependencies import SessionUser
from auth.password import verify_password
from auth.session import SESSION_COOKIE_NAME, browser_cookie_attrs
from config.settings import get_settings
from db.base import get_db
from db.redis import get_redis_client, increment_counter
from models.auth import AuditLog, User, UserOAuthProvider, WebAuthnCredential
from services.known_device_service import note_browser_sign_in
from services.security_notification_service import SecurityEvent, schedule_security_notification
from utils.datetime import utcnow
from utils.exceptions import (
    AuthenticationError,
    AuthorizationError,
    BadRequestError,
    ConfigurationError,
    ConflictError,
    NotFoundException,
    RateLimitError,
    ValidationError,
)

router = APIRouter(prefix="/auth/passkeys", tags=["authentication"])
_TTL = 300
_LIMIT = 10
_BROWSER_COOKIE = "passkey_ceremony"
_SIGN_IN_ATTEMPTS = 5  # assertions per account per _TTL window
_SIGN_IN_KEY = "webauthn_signin:{user_id}"


class Proof(BaseModel):
    current_password: str | None = Field(default=None, max_length=512)
    assertion: dict[str, Any] | None = None
    challenge: str | None = Field(default=None, max_length=128)


class CredentialResponse(BaseModel):
    challenge: str = Field(min_length=16, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    credential: dict[str, Any]


class SignIn(CredentialResponse):
    accepted_terms: str | None = Field(default=None, max_length=255)


class Rename(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class KeyInfo(BaseModel):
    id: UUID
    name: str
    created_at: datetime
    last_used_at: datetime | None


class KeyList(BaseModel):
    keys: list[KeyInfo]
    limit: int


def _check_key_limit(keys: list[WebAuthnCredential]) -> None:
    if len(keys) >= _LIMIT:
        raise ConflictError("Passkey limit reached", reason="passkey_limit", limit=_LIMIT)


def _rp() -> tuple[str, list[str]]:
    settings = get_settings()
    host = settings.webauthn_rp_id or urlsplit(settings.frontend_url).hostname
    if not host:
        raise ConfigurationError("WebAuthn relying party is not configured")
    origins = settings.webauthn_origins or settings.frontend_url
    return host, [item.strip().rstrip("/") for item in origins.split(",")]


def _handle(user: User) -> bytes:
    # A new account reclaiming an email or provider sub gets a new user handle.
    return hashlib.sha256(f"kagura-passkey:{user.id}".encode()).digest()


def _binding(request: Request, user_id: str, action: str) -> str:
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if not sid:
        raise AuthenticationError("A browser session is required")
    return hashlib.sha256(json.dumps([sid, user_id, action]).encode()).hexdigest()


async def _issue(options: Any, binding: str, state: str = "1") -> dict[str, Any]:
    public_key = json.loads(options_to_json(options))
    challenge = public_key["challenge"]
    await get_redis_client().setex(f"webauthn:{binding}:{challenge}", _TTL, state)
    return public_key


async def _consume(challenge: str, binding: str, state: str = "1") -> bytes:
    if await get_redis_client().getdel(f"webauthn:{binding}:{challenge}") != state:
        raise AuthenticationError("Invalid or expired passkey challenge")
    return base64url_to_bytes(challenge)


async def _owner(db: AsyncSession, user_id: str) -> User:
    user = await db.scalar(select(User).where(User.user_id == user_id).with_for_update())
    if not user:
        raise AuthenticationError("Account unavailable")
    return user


async def _keys(db: AsyncSession, user_id: str) -> list[WebAuthnCredential]:
    return list(
        await db.scalars(
            select(WebAuthnCredential)
            .where(WebAuthnCredential.user_id == user_id)
            .order_by(WebAuthnCredential.created_at)
        )
    )


async def _registration_state(db: AsyncSession, user: User, keys: list[WebAuthnCredential]) -> str:
    """Bind the pending proof to the account's security methods across both requests."""
    providers = await db.scalars(
        select(UserOAuthProvider.id)
        .where(UserOAuthProvider.user_id == user.user_id)
        .order_by(UserOAuthProvider.id)
    )
    state = [
        user.id,
        user.password_hash,
        user.mfa_generation,
        list(providers),
        sorted(str(k.id) for k in keys),
    ]
    return hashlib.sha256(json.dumps(state).encode()).hexdigest()


async def _assertion(
    db: AsyncSession,
    raw: dict[str, Any],
    expected: bytes,
    *,
    owner: str | None = None,
    excluding: UUID | None = None,
) -> tuple[User, WebAuthnCredential]:
    try:
        credential = parse_authentication_credential_json(raw)
        if not 1 <= len(credential.raw_id) <= 1023:
            raise AuthenticationError("Invalid credential ID")
        if parse_client_data_json(credential.response.client_data_json).cross_origin:
            raise AuthenticationError("Cross-origin passkey assertions are not accepted")
    except (ValueError, TypeError, WebAuthnException, KeyError) as exc:
        raise AuthenticationError("Invalid passkey") from exc
    query = select(WebAuthnCredential).where(WebAuthnCredential.credential_id == credential.raw_id)
    # Always lock parent before credential, like removal and account erasure.
    user_id = await db.scalar(query.with_only_columns(WebAuthnCredential.user_id))
    if not user_id or (owner is not None and owner != user_id):
        raise AuthenticationError("Invalid passkey")
    user = await _owner(db, user_id)
    key = await db.scalar(query.with_for_update())
    if (
        not key
        or key.id == excluding
        or (
            credential.response.user_handle != _handle(user)
            and (owner is None or credential.response.user_handle is not None)
        )
    ):
        raise AuthenticationError("Invalid passkey")
    # Passkey assertions draw on a counter of their own, not the password
    # sign-in budget; it is taken before the assertion is checked so concurrent
    # requests past the limit are refused without a check.
    await _reserve_sign_in_attempt(user_id)
    rp, origins = _rp()
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=expected,
            expected_rp_id=rp,
            expected_origin=origins,
            credential_public_key=key.public_key,
            credential_current_sign_count=key.sign_count,
            require_user_verification=True,
        )
    except (ValueError, TypeError, WebAuthnException) as exc:
        raise AuthenticationError("Invalid passkey") from exc
    if (
        verified.credential_device_type == CredentialDeviceType.MULTI_DEVICE
    ) != key.backup_eligible:
        raise AuthenticationError("Invalid passkey")
    key.sign_count = verified.new_sign_count
    key.backup_state = verified.credential_backed_up
    key.last_used_at = utcnow()
    return user, key


async def _reserve_sign_in_attempt(user_id: str) -> None:
    """Take one passkey attempt before the assertion is checked (one INCR).

    Args:
        user_id: The account the presented credential belongs to.

    Raises:
        RateLimitError: When the account's passkey attempts in this window are spent.
    """
    key = _SIGN_IN_KEY.format(user_id=user_id)
    if await increment_counter(key, ttl=_TTL) > _SIGN_IN_ATTEMPTS:
        raise RateLimitError("Too many passkey sign-in attempts", retry_after=_TTL)


async def _clear_sign_in_attempts(user_id: str) -> None:
    """Clear the account's passkey attempts after a verified assertion."""
    await get_redis_client().delete(_SIGN_IN_KEY.format(user_id=user_id))


async def _prove(
    db: AsyncSession,
    user: User,
    request: Request,
    proof: Proof,
    action: str,
    excluding: UUID | None = None,
) -> None:
    if proof.assertion and proof.challenge:
        challenge = await _consume(proof.challenge, _binding(request, user.user_id, action))
        await _assertion(db, proof.assertion, challenge, owner=user.user_id, excluding=excluding)
        await asyncio.to_thread(auth._clear_login_failures, auth._account_rate_key(user.user_id))
        await _clear_sign_in_attempts(user.user_id)
        return
    if proof.current_password and user.password_hash:
        if await increment_counter(f"webauthn_password:{user.user_id}", ttl=_TTL) > 5:
            raise RateLimitError("Too many verification attempts")
        if await asyncio.to_thread(verify_password, proof.current_password, user.password_hash):
            # Do not refill the sign-in budget by proving only the first factor.
            return
    elif (
        excluding is None
        and auth._session_manager
        and await asyncio.to_thread(
            auth._session_manager.proven_within,
            request.cookies.get(SESSION_COOKIE_NAME, ""),
            user.user_id,
            timedelta(seconds=_TTL),
        )
    ):
        return
    raise AuthorizationError(
        "Verify with your current password or another passkey, or sign in again"
    )


@router.get("", response_model=list[KeyInfo] | KeyList)
async def list_passkeys(
    user: SessionUser, db: AsyncSession = Depends(get_db), include_limit: bool = False
):
    """List personal passkeys; include_limit adds the server's account limit."""
    keys = await _keys(db, user["user_id"])
    return {"keys": keys, "limit": _LIMIT} if include_limit else keys


@router.post("/proof/options")
async def proof_options(
    request: Request,
    user: SessionUser,
    removing: UUID | None = None,
    db: AsyncSession = Depends(get_db),
    linking: Literal["google", "github"] | None = None,
):
    """Prove another passkey for adding or removing a credential, or for
    connecting a sign-in provider, in this session."""
    if removing and linking:
        raise ValidationError("Choose one proof purpose")
    keys = [k for k in await _keys(db, user["user_id"]) if k.id != removing]
    if not removing and not linking:
        _check_key_limit(keys)
    if not keys:
        raise ConflictError("No other passkey is available")
    options = generate_authentication_options(
        rp_id=_rp()[0],
        allow_credentials=[PublicKeyCredentialDescriptor(id=k.credential_id) for k in keys],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    if linking:
        action = f"link:{linking}"
    else:
        action = f"remove:{removing}" if removing else "add"
    return await _issue(options, _binding(request, user["user_id"], action))


@router.post("/register/options")
async def register_options(
    body: Proof, request: Request, user: SessionUser, db: AsyncSession = Depends(get_db)
):
    """Begin registration after fresh proof of the active account."""
    account = await _owner(db, user["user_id"])
    keys = await _keys(db, account.user_id)
    _check_key_limit(keys)
    await _prove(db, account, request, body, "add")
    options = generate_registration_options(
        rp_id=_rp()[0],
        rp_name=get_settings().webauthn_rp_name,
        user_id=_handle(account),
        user_name=account.email,
        user_display_name=account.name or account.email,
        exclude_credentials=[PublicKeyCredentialDescriptor(id=k.credential_id) for k in keys],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            require_resident_key=True,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    return await _issue(
        options,
        _binding(request, account.user_id, "register"),
        await _registration_state(db, account, keys),
    )


def _audit(db: AsyncSession, user: User, event: SecurityEvent) -> None:
    db.add(
        AuditLog(
            user_email=user.email, user_id=user.user_id, action=event.value, resource="passkey"
        )
    )


@router.post("/register/verify", response_model=KeyInfo, status_code=201)
async def register_verify(
    body: CredentialResponse,
    request: Request,
    user: SessionUser,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Verify registration and persist the attested public key."""
    account = await _owner(db, user["user_id"])
    keys = await _keys(db, account.user_id)
    expected = await _consume(
        body.challenge,
        _binding(request, account.user_id, "register"),
        await _registration_state(db, account, keys),
    )
    _check_key_limit(keys)
    rp, origins = _rp()
    try:
        registration = parse_registration_credential_json(body.credential)
        if not 1 <= len(registration.raw_id) <= 1023:
            raise BadRequestError("Invalid credential ID")
        if parse_client_data_json(registration.response.client_data_json).cross_origin:
            raise BadRequestError("Cross-origin passkey registration is not accepted")
        result = verify_registration_response(
            credential=registration,
            expected_challenge=expected,
            expected_rp_id=rp,
            expected_origin=origins,
            require_user_verification=True,
        )
    except (ValueError, TypeError, WebAuthnException, KeyError) as exc:
        raise BadRequestError("Invalid passkey registration") from exc
    transports = [item.value for item in registration.response.transports or []]
    key = WebAuthnCredential(
        user_id=account.user_id,
        credential_id=result.credential_id,
        public_key=result.credential_public_key,
        sign_count=result.sign_count,
        transports=transports,
        aaguid=result.aaguid,
        backup_eligible=result.credential_device_type == CredentialDeviceType.MULTI_DEVICE,
        backup_state=result.credential_backed_up,
        name="Platform passkey" if "internal" in transports else "Passkey",
    )
    db.add(key)
    _audit(db, account, SecurityEvent.PASSKEY_ADDED)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("Passkey already registered") from exc
    schedule_security_notification(
        background_tasks,
        user_id=account.user_id,
        event=SecurityEvent.PASSKEY_ADDED,
        request=request,
    )
    return key


@router.post("/authenticate/options")
async def authenticate_options(request: Request, response: Response):
    """Begin a discoverable passkey sign-in. No account identifier is required."""
    ip = auth._login_client_ip(request)
    if await increment_counter(f"webauthn_options:{ip}", ttl=60) > 60:
        raise RateLimitError("Too many sign-in requests")
    redis = get_redis_client()
    binding = request.cookies.get(_BROWSER_COOKIE, "")
    known = len(binding) == 43 and await redis.get(f"webauthn_browser:{binding}")
    if not known:
        binding = secrets.token_urlsafe(32)
    # Refresh only server-issued bindings. Each challenge has its own GETDEL.
    await redis.setex(f"webauthn_browser:{binding}", _TTL, "1")
    response.set_cookie(_BROWSER_COOKIE, binding, max_age=_TTL, **browser_cookie_attrs())
    options = await _issue(
        generate_authentication_options(
            rp_id=_rp()[0], user_verification=UserVerificationRequirement.REQUIRED
        ),
        binding,
    )
    if not known:
        # Two tabs can both start before the first cookie arrives. Keep the
        # first challenge's own cookie until its one-shot check or its expiry.
        response.set_cookie(
            _first_cookie(options["challenge"]), binding, max_age=_TTL, **browser_cookie_attrs()
        )
    return options


def _first_cookie(challenge: str) -> str:
    return f"{_BROWSER_COOKIE}_{challenge[:16]}"


@router.post("/authenticate/verify")
async def authenticate_verify(
    body: SignIn,
    request: Request,
    response: Response,
    return_to: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Verify user presence, verification, RP, origin, signature and counter before sign-in."""
    first_cookie = _first_cookie(body.challenge)
    binding = request.cookies.get(first_cookie) or request.cookies.get(_BROWSER_COOKIE)
    if not binding or not auth._session_manager:
        raise AuthenticationError("Invalid passkey sign-in")
    challenge = await _consume(body.challenge, binding)
    account, _ = await _assertion(db, body.credential, challenge)
    # Write the session while the credential and user are still locked. Removing
    # a credential cannot race an in-flight assertion into creating a later session.
    sid = await auth._create_password_session(
        account.user_id, account.email, account.name, account.role, account.picture
    )
    try:
        await db.commit()
        if not await auth._complete_password_sign_in(account.user_id, account.email, sid):
            raise AuthenticationError("Sign in again", status_code=503, error_code="AUTH-307")
        await asyncio.to_thread(auth._clear_login_failures, auth._account_rate_key(account.user_id))
        await _clear_sign_in_attempts(account.user_id)
        await auth._record_terms_acceptance(
            email=account.email,
            user_id=account.user_id,
            accepted_terms=body.accepted_terms,
            source="login",
            request=request,
        )
        if first_cookie in request.cookies:
            response.delete_cookie(first_cookie, **browser_cookie_attrs())
        auth._set_session_cookie(response, sid)
        await note_browser_sign_in(
            request, response, user_id=account.user_id, sign_in_method="Passkey"
        )
    except Exception:
        await asyncio.to_thread(auth._session_manager.delete_session, sid)
        raise
    return {"redirect_url": auth._safe_redirect_url(return_to)}


@router.patch("/{key_id}", response_model=KeyInfo)
async def rename_passkey(
    key_id: UUID, body: Rename, user: SessionUser, db: AsyncSession = Depends(get_db)
):
    """Rename one of the active account's passkeys."""
    key = await db.scalar(
        select(WebAuthnCredential)
        .where(WebAuthnCredential.id == key_id, WebAuthnCredential.user_id == user["user_id"])
        .with_for_update()
    )
    if not key:
        raise NotFoundException("Passkey")
    key.name = body.name.strip()
    if not key.name:
        raise ValidationError("Enter a passkey name")
    await db.commit()
    return key


@router.delete("/{key_id}", status_code=204)
async def remove_passkey(
    key_id: UUID,
    body: Proof,
    request: Request,
    user: SessionUser,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Remove a credential after proving a different method, retaining a way back in."""
    account = await _owner(db, user["user_id"])
    keys = await _keys(db, account.user_id)
    key = next((k for k in keys if k.id == key_id), None)
    if not key:
        raise NotFoundException("Passkey")
    if len(keys) == 1 and not account.password_hash:
        has_provider = await db.scalar(
            select(UserOAuthProvider.id)
            .where(UserOAuthProvider.user_id == account.user_id)
            .limit(1)
        )
        if not has_provider:
            raise ConflictError("Cannot remove the only remaining sign-in method")
        raise AuthorizationError(
            "Set up a password using the email link in Profile > Password, then "
            "enter it in Profile > Passkeys to remove this key. You can also add "
            "a different passkey and use it to confirm removal."
        )
    await _prove(db, account, request, body, f"remove:{key_id}", excluding=key_id)
    await db.delete(key)
    _audit(db, account, SecurityEvent.PASSKEY_REMOVED)
    await db.commit()
    schedule_security_notification(
        background_tasks,
        user_id=account.user_id,
        event=SecurityEvent.PASSKEY_REMOVED,
        request=request,
    )
    return Response(status_code=204)
