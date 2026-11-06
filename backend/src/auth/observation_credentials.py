"""Scoped read-only observation credentials and the one dependency that honours them (#2679).

An observation credential (``kagura_obs_`` + ``secrets.token_urlsafe(32)``) is
what a self-hosted dashboard holds to read the observability API. Like a share
key (#1027) it lives in its own table, so it is a fail-closed allow-list:
``get_observation_principal`` is the only code that recognises it, and only
``api/routes/observability.py`` uses that dependency. Every other route
authenticates against ``api_keys`` (or a session, OAuth token, resource
token) and therefore answers 401 to it.

The binding — one workspace, or one resource of it — comes from the row, never
from the request. Only the SHA-256 verifier is stored and it is compared with
``secrets.compare_digest``. Every credential expires (default 90 days, at most
365); rotation mints a new secret with the same binding and the remaining
lifetime and lets the old one live on for a grace period of at most a day.

The dependency checks, in order: the operator switch (404 before any header
is read), the client address allow-list (the same 404), the credential's
per-minute budget (429, fail-open on a Redis error), and the bearer itself
(one uniform 401 for absent, malformed, unknown, revoked and expired).
"""

from __future__ import annotations

import ipaddress
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from db.base import get_db
from db.redis import incrby_counter
from models.auth import Workspace
from models.observation_credential import ObservationCredential
from utils.datetime import utcnow
from utils.exceptions import (
    AuthenticationError,
    ObservabilityNotFoundError,
    ObservabilityRateLimitError,
)
from utils.hashing import sha256_hex
from utils.logger import get_logger

logger = get_logger(__name__)

OBSERVATION_KEY_PREFIX = "kagura_obs_"
OBSERVATION_KEY_DEFAULT_TTL_DAYS = 90
OBSERVATION_KEY_MAX_TTL_DAYS = 365
OBSERVATION_KEY_MIN_TTL_DAYS = 1
OBSERVATION_ROTATE_MAX_GRACE_SECONDS = 86_400
#: ``last_used_at`` is written at most once per this many seconds per credential.
OBSERVATION_LAST_USED_THROTTLE_SECONDS = 60

INVALID_CREDENTIAL_MESSAGE = "Invalid or missing observation credential"


def clamp_ttl_days(ttl_days: int | None) -> int:
    """Clamp a requested lifetime to ``[1, 365]`` days; ``None`` is 90."""
    if ttl_days is None:
        return OBSERVATION_KEY_DEFAULT_TTL_DAYS
    return max(OBSERVATION_KEY_MIN_TTL_DAYS, min(ttl_days, OBSERVATION_KEY_MAX_TTL_DAYS))


def credential_status(row: ObservationCredential, now: datetime | None = None) -> str:
    """``revoked``, ``expired`` or ``active`` for a credential row."""
    if row.revoked_at is not None:
        return "revoked"
    if (now or utcnow()) >= row.expires_at:
        return "expired"
    return "active"


@dataclass(frozen=True)
class ObservationScope:
    """What a verified observation credential may read.

    Attributes:
        credential_id: Internal row id.
        public_id: The credential's public id (``obsk_…``).
        workspace_id: The bound workspace.
        resource_pk: The bound resource (``resources.id``), or ``None`` for
            every resource of the workspace.
        expires_at: When the credential stops working (naive UTC).
    """

    credential_id: int
    public_id: str
    workspace_id: UUID
    resource_pk: UUID | None
    expires_at: datetime

    @property
    def kind(self) -> str:
        """``workspace`` or ``resource``."""
        return "resource" if self.resource_pk is not None else "workspace"


class ObservationCredentialManager:
    """Issue, verify, rotate, revoke and list observation credentials.

    Lookups by public id are always ``(public_id, workspace_id)`` (#1863): an
    owner manages the credentials of their current workspace only.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _generate() -> str:
        return f"{OBSERVATION_KEY_PREFIX}{secrets.token_urlsafe(32)}"

    async def create(
        self,
        workspace_id: UUID,
        *,
        name: str,
        resource_pk: UUID | None,
        ttl_days: int | None,
        created_by: str,
        now: datetime | None = None,
    ) -> tuple[ObservationCredential, str]:
        """Mint a credential bound to the workspace, or to one of its resources.

        The caller has already checked that ``resource_pk`` belongs to
        ``workspace_id``.

        Args:
            workspace_id: The bound workspace.
            name: Owner-chosen label.
            resource_pk: The bound resource, or ``None`` for workspace scope.
            ttl_days: Requested lifetime, clamped to ``[1, 365]``; ``None`` is 90.
            created_by: The issuing owner.
            now: Clock override for tests.

        Returns:
            ``(row, plaintext)``; the plaintext exists only in this return value.
        """
        secret = self._generate()
        row = ObservationCredential(
            key_hash=sha256_hex(secret),
            key_prefix=secret[:16],
            name=name,
            workspace_id=workspace_id,
            resource_pk=resource_pk,
            scope="resource" if resource_pk is not None else "workspace",
            created_by=created_by,
            expires_at=(now or utcnow()) + timedelta(days=clamp_ttl_days(ttl_days)),
        )
        self.db.add(row)
        await self.db.flush()
        logger.info(
            "observation_credential_created",
            public_id=row.public_id,
            workspace_id=str(workspace_id),
            scope=row.scope,
            expires_at=row.expires_at.isoformat(),
        )
        return row, secret

    async def verify(self, secret: str, *, now: datetime | None = None) -> ObservationScope | None:
        """Return the scope of a valid credential, else ``None`` (every rejection alike).

        Args:
            secret: The presented bearer value.
            now: Clock override for tests.

        Returns:
            The credential's scope when it has the prefix, is known, is not
            revoked and has not expired.
        """
        if not secret.startswith(OBSERVATION_KEY_PREFIX):
            return None
        key_hash = sha256_hex(secret)
        # A credential of a deleted workspace reads nothing.
        row = (
            await self.db.execute(
                select(ObservationCredential)
                .join(Workspace, Workspace.id == ObservationCredential.workspace_id)
                .where(
                    ObservationCredential.key_hash == key_hash,
                    Workspace.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        # Constant-time compare before trusting the row (#964).
        if row is None or not secrets.compare_digest(row.key_hash, key_hash):
            return None
        current = now or utcnow()
        if credential_status(row, current) != "active":
            return None
        throttle = timedelta(seconds=OBSERVATION_LAST_USED_THROTTLE_SECONDS)
        if row.last_used_at is None or current - row.last_used_at >= throttle:
            row.last_used_at = current
            await self.db.flush()
        return ObservationScope(
            credential_id=row.id,
            public_id=row.public_id,
            workspace_id=row.workspace_id,
            resource_pk=row.resource_pk,
            expires_at=row.expires_at,
        )

    async def _get(
        self, public_id: str, workspace_id: UUID, *, for_update: bool = False
    ) -> ObservationCredential | None:
        stmt = select(ObservationCredential).where(
            ObservationCredential.public_id == public_id,
            ObservationCredential.workspace_id == workspace_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def rotate(
        self,
        public_id: str,
        workspace_id: UUID,
        *,
        grace_seconds: int = 0,
        created_by: str,
        now: datetime | None = None,
    ) -> tuple[ObservationCredential, str] | None:
        """Replace a live credential with a new secret of the same binding.

        The new row keeps the name, the binding and the old row's expiry (the
        remaining lifetime) and points back at it. The old row then expires at
        ``min(expires_at, now + grace_seconds)``.

        Args:
            public_id: The credential to rotate.
            workspace_id: The caller's workspace.
            grace_seconds: How long the old secret keeps working, 0..86400.
            created_by: The rotating owner.
            now: Clock override for tests.

        Returns:
            ``(new_row, plaintext)``, or ``None`` when no live credential with
            that id is in the workspace, or it was already rotated (one
            successor per credential: a double submit, or rotating the old id
            again during its grace period, would leave a second live child).
        """
        current = now or utcnow()
        # The row lock serialises concurrent rotations of the same credential.
        old = await self._get(public_id, workspace_id, for_update=True)
        if old is None or credential_status(old, current) != "active":
            return None
        successor = (
            await self.db.execute(
                select(ObservationCredential.id)
                .where(ObservationCredential.rotated_from_id == old.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if successor is not None:
            return None
        grace = max(0, min(grace_seconds, OBSERVATION_ROTATE_MAX_GRACE_SECONDS))
        secret = self._generate()
        new = ObservationCredential(
            key_hash=sha256_hex(secret),
            key_prefix=secret[:16],
            name=old.name,
            workspace_id=old.workspace_id,
            resource_pk=old.resource_pk,
            scope=old.scope,
            created_by=created_by,
            expires_at=old.expires_at,
            rotated_from_id=old.id,
        )
        old.expires_at = min(old.expires_at, current + timedelta(seconds=grace))
        self.db.add(new)
        await self.db.flush()
        logger.info(
            "observation_credential_rotated",
            public_id=new.public_id,
            rotated_from=old.public_id,
            workspace_id=str(workspace_id),
            grace_seconds=grace,
        )
        return new, secret

    async def revoke(
        self, public_id: str, workspace_id: UUID, *, now: datetime | None = None
    ) -> bool:
        """Revoke a credential of the workspace; revoking it again is a no-op.

        Returns:
            ``False`` when the workspace has no credential with that id.
        """
        row = await self._get(public_id, workspace_id)
        if row is None:
            return False
        if row.revoked_at is None:
            row.revoked_at = now or utcnow()
            await self.db.flush()
            logger.info(
                "observation_credential_revoked",
                public_id=public_id,
                workspace_id=str(workspace_id),
            )
        return True

    async def list_for_workspace(self, workspace_id: UUID) -> list[ObservationCredential]:
        """Every credential of the workspace, revoked and expired ones included, newest first."""
        result = await self.db.execute(
            select(ObservationCredential)
            .where(ObservationCredential.workspace_id == workspace_id)
            .order_by(ObservationCredential.created_at.desc(), ObservationCredential.id.desc())
        )
        return list(result.scalars().all())


def _client_allowed(request: Request) -> bool:
    networks = get_settings().observability_api_allowed_networks
    if not networks:
        return True
    host = request.client.host if request.client else None
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    # A dual-stack listener reports an IPv4 caller as ``::ffff:a.b.c.d``.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in networks)


async def _check_rate_limit(key_hash: str) -> None:
    """Count one request against the presented credential's minute bucket.

    Keyed by the verifier of the presented secret, so a known credential
    over its budget is refused before the database is asked. An unknown
    secret gets a fresh bucket (one key living a minute) and then the
    uniform 401; this budget meters credentials, it does not throttle
    guessing (a 256-bit secret is not guessable). Fail-open: the API is
    internal and authenticated, so a Redis outage serves the request.
    """
    limit = get_settings().observability_api_rate_limit_per_minute
    minute = utcnow().strftime("%Y%m%d%H%M")
    try:
        count = await incrby_counter(f"obs_rl:{key_hash[:32]}:{minute}", 1, ttl=60)
    except Exception as exc:
        logger.warning("observability_rate_limit_unavailable", error_type=type(exc).__name__)
        return
    if count > limit:
        raise ObservabilityRateLimitError()


async def get_observation_principal(
    request: Request, db: AsyncSession = Depends(get_db)
) -> ObservationScope:
    """Authenticate the observability API's caller (the only reader of ``kagura_obs_``).

    Raises:
        ObservabilityNotFoundError: The API is switched off, or the client
            address is outside the allow-list (404, before ``Authorization``
            is read).
        ObservabilityRateLimitError: The credential's minute budget is spent (429).
        AuthenticationError: No valid observation credential (401, one message).
    """
    if not get_settings().observability_api_enabled:
        raise ObservabilityNotFoundError()
    if not _client_allowed(request):
        raise ObservabilityNotFoundError()

    authorization = request.headers.get("authorization") or ""
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token.startswith(OBSERVATION_KEY_PREFIX):
        raise AuthenticationError(INVALID_CREDENTIAL_MESSAGE)

    await _check_rate_limit(sha256_hex(token))

    scope = await ObservationCredentialManager(db).verify(token)
    if scope is None:
        raise AuthenticationError(INVALID_CREDENTIAL_MESSAGE)
    # verify may have flushed last_used_at; a failed commit only loses that.
    try:
        await db.commit()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("observation_last_used_commit_failed", error_type=type(exc).__name__)
        await db.rollback()
    return scope
