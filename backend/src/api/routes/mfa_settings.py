"""Enroll or remove a password account's second factor from its browser session."""

import asyncio
import json
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes import auth
from api.routes.password import _CURRENT_PASSWORD_ATTEMPTS_PER_USER, _over_limit, _session_revoker
from auth.dependencies import SessionUser
from auth.password import verify_password
from auth.recovery_codes import issue_codes, take_locked_code
from auth.session import SESSION_COOKIE_NAME
from auth.totp import generate_totp_secret, get_provisioning_uri, verify_totp
from db.base import get_db
from models.auth import AuditLog, User
from services.password_account_service import credential_fingerprint
from services.security_notification_service import SecurityEvent, schedule_security_notification
from utils.encryption import get_encryptor
from utils.exceptions import (
    AuthenticationError,
    ConflictError,
    CurrentPasswordMismatchError,
    RateLimitError,
)
from utils.hashing import sha256_hex

router = APIRouter(prefix="/auth/mfa", tags=["authentication"])


class SetupBody(BaseModel):
    current_password: SecretStr


class CodeBody(BaseModel):
    code: str = Field(min_length=1, max_length=35)


class DisableBody(SetupBody, CodeBody):
    pass


class Enrollment(BaseModel):
    secret: str
    provisioning_uri: str


class RecoveryCodes(BaseModel):
    recovery_codes: list[str]


async def _account(db: AsyncSession, session: SessionUser) -> User:
    account = await db.scalar(
        select(User).where(User.user_id == session["user_id"]).with_for_update()
    )
    if not account or not account.password_hash:
        raise ConflictError("A password is required to manage two-factor authentication")
    return account


async def _check_password(account: User, password: SecretStr) -> None:
    if await _over_limit(f"pw_current_user:{account.user_id}", _CURRENT_PASSWORD_ATTEMPTS_PER_USER):
        raise RateLimitError("Too many attempts. Please try again later.")
    if not account.password_hash or not await asyncio.to_thread(
        verify_password, password.get_secret_value(), account.password_hash
    ):
        raise CurrentPasswordMismatchError()


def _manager():
    if not auth._session_manager:
        raise HTTPException(503, "Session store unavailable")
    return auth._session_manager


def _pending_key(account: User, request: Request) -> str:
    # Binding to the actual session also prevents an abandoned enrollment on a
    # shared computer from being finished under another login.
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        raise AuthenticationError("A browser session is required")
    return f"mfa_enroll:{account.user_id}:{sha256_hex(cookie)}"


async def _commit_change(
    account: User, db: AsyncSession, request: Request, tasks: BackgroundTasks, event: SecurityEvent
) -> None:
    # Invalidate password sign-ins already in flight as well as other sessions.
    account.mfa_generation = uuid4().hex
    db.add(
        AuditLog(
            user_id=account.user_id,
            user_email="self-service",
            resource="user:mfa",
            action=event.value,
            ip_address=auth._login_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    )
    await asyncio.to_thread(
        _session_revoker(request.cookies.get(SESSION_COOKIE_NAME)), account.user_id
    )
    await db.commit()
    auth._clear_login_failures(auth._account_rate_key(account.user_id))
    schedule_security_notification(tasks, user_id=account.user_id, event=event, request=request)


@router.post("/setup", response_model=Enrollment)
async def setup_mfa(
    body: SetupBody,
    request: Request,
    response: Response,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> Enrollment:
    """Reauthenticate with the current password. Enrollment expires in five minutes."""
    account = await _account(db, user)
    await _check_password(account, body.current_password)
    if account.totp_enabled:
        raise ConflictError("Two-factor authentication is already enabled")
    secret = generate_totp_secret()
    pending = json.dumps(
        [secret, credential_fingerprint(account.password_hash, account.mfa_generation)]
    )
    _manager()._redis.setex(_pending_key(account, request), 300, get_encryptor().encrypt(pending))
    response.headers["Cache-Control"] = "no-store"
    return Enrollment(secret=secret, provisioning_uri=get_provisioning_uri(secret, account.email))


@router.post("/enable", response_model=RecoveryCodes)
async def enable_mfa(
    body: CodeBody,
    request: Request,
    response: Response,
    user: SessionUser,
    tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> RecoveryCodes:
    """Confirm the pending TOTP, then show the account's one-time recovery codes once."""
    account = await _account(db, user)
    if account.totp_enabled:
        raise ConflictError("Two-factor authentication is already enabled")
    manager = _manager()
    key = _pending_key(account, request)
    encrypted = manager._redis.get(key)
    if not encrypted:
        raise ConflictError("Enrollment expired. Start setup again.")
    secret, fingerprint = json.loads(get_encryptor().decrypt(encrypted))
    if fingerprint != credential_fingerprint(account.password_hash, account.mfa_generation):
        raise ConflictError("Credentials changed. Start setup again.")
    auth._reserve_login_attempt(auth._account_rate_key(account.user_id))
    if not verify_totp(secret, body.code) or not auth._claim_totp_code(account.user_id, body.code):
        raise AuthenticationError("Invalid or already used verification code")
    codes, account.totp_recovery_hashes = issue_codes()
    account.totp_secret = get_encryptor().encrypt(secret)
    account.totp_enabled = True
    manager._redis.delete(key)
    await _commit_change(account, db, request, tasks, SecurityEvent.MFA_ENABLED)
    response.headers["Cache-Control"] = "no-store"
    return RecoveryCodes(recovery_codes=codes)


@router.post("/disable", status_code=204, response_class=Response)
async def disable_mfa(
    body: DisableBody,
    request: Request,
    user: SessionUser,
    tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Remove MFA with the current password and a TOTP or single-use recovery code."""
    account = await _account(db, user)
    await _check_password(account, body.current_password)
    if not account.totp_enabled or not account.totp_secret:
        raise ConflictError("Two-factor authentication is not enabled")
    _manager()
    auth._reserve_login_attempt(auth._account_rate_key(account.user_id))
    if not take_locked_code(account, body.code):
        secret = get_encryptor().decrypt(account.totp_secret)
        if not verify_totp(secret, body.code) or not auth._claim_totp_code(
            account.user_id, body.code
        ):
            raise AuthenticationError("Invalid or already used verification code")
    account.totp_enabled = False
    account.totp_secret = None
    account.totp_recovery_hashes = None
    await _commit_change(account, db, request, tasks, SecurityEvent.MFA_DISABLED)
    return Response(status_code=204)
