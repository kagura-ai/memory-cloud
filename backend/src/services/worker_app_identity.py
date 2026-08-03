"""Lifecycle service for worker app identities (#1315)."""

from __future__ import annotations

import re
from datetime import timedelta
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.worker_app import WorkerAppIdentity
from utils.datetime import utcnow
from utils.exceptions import ConflictError, NotFoundException, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

MAX_RETIRING_WINDOW_SECONDS = 86_400

# Per-platform signing-secret shape (#1478).
#
# A placeholder string was once submitted to the rotate endpoint, accepted with
# 200, encrypted and stored as the ACTIVE secret. Every webhook then failed
# verification, and when the retiring window elapsed there was no value left
# that could verify anything. The field's only constraint was `min_length=1`.
#
# Validation lives here rather than on the request models because `platform` is
# a path parameter on rotate and a body field on create — only the service sees
# it uniformly. Both routes funnel through this module, so a caller cannot
# bypass the check by picking a different entry point, and neither can a future
# UI: a form validator is advisory, this is the boundary.
#
# An unknown platform keeps the caller's length bounds and no shape, so adding
# Discord/Teams is additive and never rejects a valid credential for a format
# that has not been described yet.
_SIGNING_SECRET_SHAPES: dict[str, re.Pattern[str]] = {
    # Slack signing secrets are 32 lowercase hex characters.
    "slack": re.compile(r"^[0-9a-f]{32}$"),
}


def validate_signing_secret(platform: str, signing_secret: str) -> None:
    """Reject a secret that cannot be a credential for ``platform``.

    Raises :class:`ValidationError`. The message carries the expected shape and
    the OBSERVED LENGTH only — never the submitted value. An admin who pastes
    the wrong thing must not get it reflected back into a response body or a
    log line, because the wrong thing is very often the right secret for
    somewhere else.
    """
    shape = _SIGNING_SECRET_SHAPES.get(platform)
    if shape is None:
        return
    if shape.fullmatch(signing_secret) is None:
        raise ValidationError(
            f"signing_secret for platform {platform!r} must match "
            f"{shape.pattern} (received {len(signing_secret)} characters)",
            field="signing_secret",
        )


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
        validate_signing_secret(platform, signing_secret)
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
        config_changed = False
        if display_name is not None:
            identity.display_name = display_name
        if status is not None:
            if status == "active" and not identity.active_signing_secret_encrypted:
                raise ValidationError(
                    "A signing secret must be configured before enabling an app identity",
                    field="status",
                )
            if status == "active" and status != identity.status:
                # #1356: enabling a row whose stored ciphertext no longer
                # decrypts (Fernet key rotation / corruption) would return
                # 200 while the fleet silently serves the app secretless.
                # Fail loudly and point the operator at the recovery path.
                try:
                    identity.get_active_signing_secret()
                except ValueError as exc:
                    raise ValidationError(
                        "The stored signing secret cannot be decrypted; "
                        "rotate the secret before enabling this app identity",
                        field="status",
                    ) from exc
            # #1356: a real status transition tears down the retiring window.
            # Disabling revokes ALL acceptance (sticky revocation — the armed
            # previous secret must not survive to be revived by a later
            # re-enable), and enabling purges retiring material a row may
            # still carry from before this rule existed. A same-status
            # PATCH to "disabled" also purges (operator lever to scrub
            # revoked material from legacy rows without re-enabling); an
            # active→active PATCH is not a transition and keeps a
            # legitimately armed rotation window.
            if status != identity.status or status == "disabled":
                # Any of the three window columns set → audit (the trio is
                # one invariant; a half-populated legacy row must not be
                # cleared silently). Copilot review on #1361.
                if (
                    identity.retiring_secret_revision is not None
                    or identity.retiring_signing_secret_encrypted is not None
                    or identity.retiring_valid_until is not None
                ):
                    # Audit the teardown BEFORE clearing so the #1343
                    # correlation (which retiring revision a disable
                    # revoked) survives — the route only sees post-clear
                    # state. Ids/enums only, never secret material.
                    logger.info(
                        "worker_app_retiring_window_cleared",
                        platform=identity.platform,
                        app_key=identity.app_key,
                        from_status=identity.status,
                        to_status=status,
                        retiring_secret_revision=identity.retiring_secret_revision,
                    )
                identity.clear_retiring_secret()
            if status != identity.status:
                config_changed = True
            identity.status = status
        identity.updated_by = actor_id
        # #1360: display_name is admin-display-only — it is not part of the
        # worker-facing config, so a rename must not bump config_version
        # (the bump changes identity_collection_revision and makes EVERY
        # worker refetch its config on the next bootstrap poll). Only a
        # real status transition is config-relevant here; rotate_secret
        # bumps on its own.
        if config_changed:
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
        # Validate BEFORE any mutation: a rejected rotation must leave the
        # existing active/retiring pair exactly as it was. #1478's outage was
        # made worse by the bad value displacing the working one.
        validate_signing_secret(platform, signing_secret)
        if not 0 <= retiring_for_seconds <= MAX_RETIRING_WINDOW_SECONDS:
            raise ValidationError(
                f"retiring_for_seconds must be between 0 and {MAX_RETIRING_WINDOW_SECONDS}",
                field="retiring_for_seconds",
            )
        identity = await self._require_identity(platform, app_key)
        # #1356: rotate is the documented recovery path when the stored
        # ciphertext can no longer be decrypted (Fernet key rotation,
        # corruption) — it must not 500 on the OLD material it is about to
        # replace. Drop the unrecoverable previous secret (no retiring
        # window; the fleet could never verify against it anyway) and let
        # the new-secret write below BE the recovery. Ids/enums only in the
        # audit event — never secret material or ciphertext.
        try:
            previous_secret = identity.get_active_signing_secret()
        except ValueError as exc:
            # utils.encryption.decrypt maps every real decryption failure
            # (InvalidToken: wrong key / tampered) to ValueError — same
            # contract the bootstrap lane in routes/workers.py relies on.
            # Anything else is a programming error and must keep surfacing.
            previous_secret = None
            logger.warning(
                "worker_app_previous_secret_undecryptable",
                platform=identity.platform,
                app_key=identity.app_key,
                active_secret_revision=identity.active_secret_revision,
                error_type=type(exc).__name__,
            )
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
            identity.clear_retiring_secret()

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
