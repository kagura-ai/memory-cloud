"""The approved public text of a public MCP endpoint, and who may change it (#2995).

Two routes change it, through one function (``apply_public_text``), so the
limits, the blank handling and the audit record are the same by construction:

- the workspace owner's ``PATCH /api/v1/resources/{resource_id}/public-mcp``
  (name, description and usage guide);
- a managing application acting for the canonical owner through an
  installation's registered browser client
  (``POST …/sync-installations/{installation_id}/publication-text``,
  experimental): description and usage guide only; the public name stays the
  owner's.

The application's command admits the caller exactly as the custom-domain
commands do (``sync_publication_domain_commands.admitted_command``): the
canonical owner and ``OWNER`` member with a live ``integrations:manage``
token of the installation's browser client, the installation of the path,
not unlinked, and its binding exactly as the command names it. The endpoint
it edits is the one that binding names, which the admission compared with the
installation row; a request cannot name another. Every refusal is the
synchronization controls' fixed answer. Nothing here consumes a paid right,
creates, republishes or rebinds a publication.

The ``integration.updated`` record names the owner as actor; on the
application's command it also carries the acting OIDC client
(``principal_kind="oauth"``, ``client_id``). The text itself is never logged.
"""

from __future__ import annotations

import hashlib
from typing import Final, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.history_principal import set_history_principal
from auth.sync_identity import SyncPrincipal
from models.public_mcp import (
    PUBLIC_DESCRIPTION_MAX_CHARS,
    PUBLIC_NAME_MAX_CHARS,
    PUBLIC_USAGE_GUIDE_MAX_CHARS,
    PublicMcpEndpoint,
)
from models.sync_publication_text_schemas import (
    PublicationTextResult,
    PublicationTextUpdate,
    PublicTextField,
)
from services.operation_history_writer import record_operation
from services.sync_publication_domain_commands import DomainCommand, admitted_command
from utils.datetime import utcnow
from utils.db_helpers import is_lock_not_available
from utils.exceptions import NotFoundException, SyncLockBusyError, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)


# operation_history.client_id holds 48 characters; a registered OIDC client id
# may have up to 128 (cli/oidc_client.py).
_HISTORY_CLIENT_ID_MAX: Final = 48


def history_client_id(client_id: str) -> str:
    """The client id as the operation record stores it: as is when it fits.

    A longer id is stored as ``sha256:`` and the first hex digits of its
    digest, 48 characters in all: stable per client and never cut into
    another client's id. The full id stays in the synchronization audit row
    of the same request.
    """
    if len(client_id) <= _HISTORY_CLIENT_ID_MAX:
        return client_id
    digest = hashlib.sha256(client_id.encode("utf-8")).hexdigest()
    return "sha256:" + digest[: _HISTORY_CLIENT_ID_MAX - len("sha256:")]


class _Keep:
    """An omitted field: its value stays."""


KEEP: Final = _Keep()


def blank_to_none(value: str | None) -> str | None:
    """An empty or whitespace-only text is stored as NULL."""
    return value if value and value.strip() else None


def _bounded(value: str | None, limit: int, field: str) -> str | None:
    if value is not None and len(value) > limit:
        raise ValidationError(f"{field} is longer than {limit} characters", field=field)
    return value


def apply_public_text(
    db: AsyncSession,
    row: PublicMcpEndpoint,
    *,
    actor_user_id: str,
    name: str | None | _Keep = KEEP,
    description: str | None | _Keep = KEEP,
    usage_guide: str | None | _Keep = KEEP,
) -> list[str]:
    """Apply an edit of the approved public text; return the changed fields.

    ``KEEP`` leaves a field as it is. A blank description or usage guide
    clears it; the name cannot be cleared. The owner API's limits hold here
    too. When something changed, ``updated_at`` is set and one
    ``integration.updated`` record names the changed fields (``name``,
    ``description``, ``usage_guide``); the caller flushes and commits.
    """
    previous = (row.public_name, row.public_description, row.public_usage_guide)
    if not isinstance(name, _Keep):
        if name is None or not name.strip():
            raise ValidationError("public_name must not be blank", field="public_name")
        row.public_name = _bounded(name.strip(), PUBLIC_NAME_MAX_CHARS, "public_name") or ""
    if not isinstance(description, _Keep):
        row.public_description = blank_to_none(
            _bounded(description, PUBLIC_DESCRIPTION_MAX_CHARS, "public_description")
        )
    if not isinstance(usage_guide, _Keep):
        row.public_usage_guide = blank_to_none(
            _bounded(usage_guide, PUBLIC_USAGE_GUIDE_MAX_CHARS, "public_usage_guide")
        )
    changed_fields = [
        field
        for field, old, new in zip(
            ("name", "description", "usage_guide"),
            previous,
            (row.public_name, row.public_description, row.public_usage_guide),
            strict=True,
        )
        if old != new
    ]
    if changed_fields:
        row.updated_at = utcnow()
        record_operation(
            db,
            operation="integration.updated",
            actor_user_id=actor_user_id,
            workspace_id=row.workspace_id,
            context_id=row.context_id,
            target_id=row.public_id,
            subject_user_id=row.created_by,
            changed_fields=changed_fields,
        )
    return changed_fields


async def update_installation_public_text(
    db: AsyncSession,
    workspace_id: UUID,
    installation_id: UUID,
    principal: SyncPrincipal,
    body: PublicationTextUpdate,
) -> PublicationTextResult:
    """Set the public text of the installation's endpoint for its canonical owner (experimental).

    One transaction: the admission (row locks ``NOWAIT``, see
    ``admitted_command``), then the endpoint row ``FOR UPDATE NOWAIT`` (a held
    row is ``RES-002``), the edit and its record, the commit. An endpoint that
    is no longer live is the uniform 404.
    """
    command = DomainCommand(workspace_id, installation_id, principal, body.expected)
    fields = body.model_fields_set
    async with admitted_command(db, command, paid_guarded=False):
        try:
            row = await db.scalar(
                select(PublicMcpEndpoint)
                .where(
                    PublicMcpEndpoint.id == body.expected.endpoint_id,
                    PublicMcpEndpoint.workspace_id == workspace_id,
                    PublicMcpEndpoint.revoked_at.is_(None),
                )
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
        except DBAPIError as exc:
            if is_lock_not_available(exc):
                raise SyncLockBusyError() from exc
            raise
        if row is None:
            raise NotFoundException("Publication target")
        # The operation record names the acting OIDC client (#2995).
        set_history_principal("oauth", client_id=history_client_id(principal.client_id))
        changed = apply_public_text(
            db,
            row,
            actor_user_id=principal.user_id,
            description=body.public_description if "public_description" in fields else KEEP,
            usage_guide=body.public_usage_guide if "public_usage_guide" in fields else KEEP,
        )
        await db.flush()
        result = PublicationTextResult(
            endpoint_id=row.id,
            public_id=row.public_id,
            public_description=row.public_description,
            public_usage_guide=row.public_usage_guide,
            updated_at=row.updated_at,
            changed_fields=cast(list[PublicTextField], [f for f in changed if f != "name"]),
        )
        await db.commit()
    logger.info(
        "public_mcp_endpoint_text_set_by_installation",
        workspace_id=str(workspace_id),
        installation_id=str(installation_id),
        endpoint_id=str(result.endpoint_id),
        changed=len(result.changed_fields),
    )
    return result


__all__ = [
    "KEEP",
    "PublicTextField",
    "apply_public_text",
    "blank_to_none",
    "history_client_id",
    "update_installation_public_text",
]
