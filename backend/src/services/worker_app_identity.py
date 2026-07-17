"""Lifecycle service for worker app identities (#1315)."""

from __future__ import annotations

from datetime import timedelta
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.worker_app import WorkerAppIdentity
from utils.datetime import utcnow
from utils.exceptions import ConflictError, NotFoundException, ValidationError

MAX_RETIRING_WINDOW_SECONDS = 86_400


def opaque_revision(*parts: object) -> str:
    """Return a stable non-semantic revision safe for ETag/API exposure."""
    material = "\x1f".join(str(part) for part in parts)
    return sha256(material.encode()).hexdigest()[:32]


def identity_revision(identity: WorkerAppIdentity) -> str:
    return opaque_revision(identity.id, identity.config_version, identity.status)


def identity_collection_revision(identities: list[WorkerAppIdentity]) -> str:
    now = utcnow()
    return opaque_revision(
        *(
            opaque_revision(
                identity_revision(identity),
                bool(
                    identity.status == "active"
                    and identity.retiring_valid_until
                    and identity.retiring_valid_until > now
                ),
            )
            for identity in identities
        ),
        "empty" if not identities else "apps",
    )


class WorkerAppIdentityService:
    """Manage platform app identities without exposing stored ciphertext."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_identities(self, *, active_only: bool = False) -> list[WorkerAppIdentity]:
        statement = select(WorkerAppIdentity)
        if active_only:
            statement = statement.where(WorkerAppIdentity.status == "active")
        result = await self.db.execute(
            statement.order_by(WorkerAppIdentity.platform, WorkerAppIdentity.app_key)
        )
        return list(result.scalars().all())

    async def get_identity(
        self, platform: str, app_key: str, *, for_update: bool = False
    ) -> WorkerAppIdentity | None:
        statement = select(WorkerAppIdentity).where(
            WorkerAppIdentity.platform == platform,
            WorkerAppIdentity.app_key == app_key,
        )
        if for_update:
            # populate_existing is load-bearing (same pattern as
            # workspace_locks.py): if the row is already in this session's
            # identity map, a bare FOR UPDATE re-read would return the stale
            # cached instance — the lock would be held while mutating
            # attributes read before the lock. Force a refresh under the lock.
            statement = statement.with_for_update().execution_options(populate_existing=True)
        result = await self.db.execute(statement)
        return result.scalar_one_or_none()

    async def create_identity(
        self,
        *,
        platform: str,
        app_key: str,
        display_name: str,
        signing_secret: str,
        actor_id: str,
    ) -> WorkerAppIdentity:
        if await self.get_identity(platform, app_key) is not None:
            raise ConflictError("Worker app identity already exists")
        identity = WorkerAppIdentity(
            platform=platform,
            app_key=app_key,
            display_name=display_name,
            status="active",
            active_secret_revision=1,
            config_version=1,
            created_by=actor_id,
            updated_by=actor_id,
        )
        identity.set_active_signing_secret(signing_secret)
        self.db.add(identity)
        await self.db.flush()
        return identity

    async def update_identity(
        self,
        *,
        platform: str,
        app_key: str,
        actor_id: str,
        display_name: str | None = None,
        status: str | None = None,
    ) -> WorkerAppIdentity:
        identity = await self._require_identity(platform, app_key)
        if display_name is not None:
            identity.display_name = display_name
        if status is not None:
            if status == "active" and not identity.active_signing_secret_encrypted:
                raise ValidationError(
                    "A signing secret must be configured before enabling an app identity",
                    field="status",
                )
            identity.status = status
        identity.updated_by = actor_id
        identity.config_version += 1
        await self.db.flush()
        return identity

    async def rotate_secret(
        self,
        *,
        platform: str,
        app_key: str,
        signing_secret: str,
        retiring_for_seconds: int,
        actor_id: str,
    ) -> WorkerAppIdentity:
        if not 0 <= retiring_for_seconds <= MAX_RETIRING_WINDOW_SECONDS:
            raise ValidationError(
                f"retiring_for_seconds must be between 0 and {MAX_RETIRING_WINDOW_SECONDS}",
                field="retiring_for_seconds",
            )
        identity = await self._require_identity(platform, app_key)
        previous_secret = identity.get_active_signing_secret()
        previous_revision = identity.active_secret_revision
        # Arm the retiring window only when the identity is currently ACTIVE.
        # On a disabled identity the previous secret is revoked material — it
        # must not be re-served to the fleet if the identity is later
        # re-enabled; on an unconfigured identity there is no fleet to keep
        # verifying against the old value.
        if (
            identity.status == "active"
            and previous_secret
            and previous_revision
            and retiring_for_seconds > 0
        ):
            identity.set_retiring_signing_secret(previous_secret)
            identity.retiring_secret_revision = previous_revision
            identity.retiring_valid_until = utcnow() + timedelta(seconds=retiring_for_seconds)
        else:
            identity.set_retiring_signing_secret(None)
            identity.retiring_secret_revision = None
            identity.retiring_valid_until = None

        identity.set_active_signing_secret(signing_secret)
        identity.active_secret_revision = (previous_revision or 0) + 1
        # Rotation replaces secret material only — it never changes status.
        # Enabling stays the explicit update_identity(status="active") step:
        # revocation must be sticky (rotating a disabled identity must not
        # resurrect it), and rotating a secret into the migration-window
        # 'unconfigured' default must not flip config dispatch from the
        # worker-env path to identity-governed until the operator says so.
        identity.updated_by = actor_id
        identity.config_version += 1
        await self.db.flush()
        return identity

    async def _require_identity(self, platform: str, app_key: str) -> WorkerAppIdentity:
        # Serialize lifecycle mutations so concurrent rotations cannot reuse a
        # revision or accidentally resurrect an older active secret.
        identity = await self.get_identity(platform, app_key, for_update=True)
        if identity is None:
            raise NotFoundException("Worker app identity")
        return identity
