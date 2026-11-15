"""Versioned, content-free synchronization audit using the existing erasure boundary."""

from asyncio import timeout
from collections.abc import Awaitable, Callable, Coroutine
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal, get_args
from uuid import UUID, uuid4

from fastapi import Request, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from config.settings import get_settings
from db.base import isolated_session
from models.auth import AuditLog, User
from models.sync_activity import SyncActivityAction, SyncManualRequest
from models.sync_installation import SyncInstallation
from utils.datetime import to_utc_iso, utcnow
from utils.logger import get_logger

if TYPE_CHECKING:
    from services.sync_credentials import CredentialAuthority

RETENTION_DAYS = 90
SCHEMA = "sync-activity-v1"
ACTIONS = frozenset(get_args(SyncActivityAction))

logger = get_logger(__name__)


@dataclass(frozen=True)
class AuditActor:
    """Identity obtained from verified KMC authority, never a request header."""

    kind: Literal["human", "service", "operator", "system"]
    subject: str
    client_id: str | None = None
    issuer: str | None = None
    account_id: int | None = field(default=None, repr=False)
    key_id: UUID | None = None


@dataclass
class AuditRequest:
    """One task-local request's most recently verified authority and fixed target."""

    actor: AuditActor | None = None
    installation: "AuditTarget | None" = None
    db: AsyncSession | None = None
    correlation_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True)
class AuditTarget:
    """Scalar identity snapshot which remains safe to use after transaction rollback."""

    id: UUID
    workspace_id: UUID
    service_id: str
    resource_pk: UUID
    context_id: UUID
    endpoint_id: UUID
    generation: int


_request: ContextVar[AuditRequest | None] = ContextVar("sync_audit_request", default=None)


def begin_request() -> Token:
    """Open a request-local identity slot; nested HTTP calls cannot inherit authority."""
    return _request.set(AuditRequest())


def end_request(token: Token) -> None:
    """Restore the parent context after all response and error paths."""
    _request.reset(token)


def human_actor(user_id: str, client_id: str, *, account_id: int | None = None) -> AuditActor:
    """Use the canonical KMC subject which existing account erasure can unlink."""
    request = _request.get()
    if (
        account_id is None
        and request is not None
        and request.actor is not None
        and request.actor.kind == "human"
        and request.actor.subject == user_id
        and request.actor.client_id == client_id
    ):
        account_id = request.actor.account_id
    return AuditActor("human", user_id, client_id, get_settings().oidc_issuer, account_id)


def bind_actor(
    actor: AuditActor, db: AsyncSession, installation: SyncInstallation | None = None
) -> None:
    """Only call after server-side authentication; this grants no new authority."""
    request = _request.get()
    if request is not None:
        request.actor, request.db = actor, db
        if installation is not None:
            request.installation = AuditTarget(
                installation.id,
                installation.workspace_id,
                installation.service_id,
                installation.resource_pk,
                installation.context_id,
                installation.endpoint_id,
                installation.generation,
            )


def service_actor(authority: "CredentialAuthority") -> AuditActor:
    """Keep the actual worker client distinct from any human requesting the job."""
    return AuditActor(
        "service",
        "service:" + authority.client.client_id,
        authority.client.client_id,
        authority.issuer,
        key_id=authority.key.id,
    )


def current_actor(subject: str | None) -> AuditActor:
    """A mode transition may be human-triggered or performed by maintenance."""
    request = _request.get()
    if (
        subject is not None
        and request is not None
        and request.actor is not None
        and request.actor.subject == subject
    ):
        return request.actor
    return AuditActor("human" if subject else "system", subject or "service:sync-management")


def audited_http(
    handler: Callable[[Request], Awaitable[Response]],
) -> Callable[[Request], Coroutine[Any, Any, Response]]:
    """Capture an authenticated refusal after the handler has rolled back its mutation."""

    async def wrapped(request: Request) -> Response:
        token = begin_request()
        try:
            response = await handler(request)
            await record_denial(response.status_code)
            return response
        finally:
            end_request(token)

    return wrapped


def add_audit(
    db: AsyncSession | Session,
    action: str,
    actor: AuditActor,
    *,
    installation: SyncInstallation | AuditTarget | None = None,
    workspace_id: UUID | None = None,
    service_id: str | None = None,
    authorization_id: UUID | None = None,
    feed_id: UUID | None = None,
    run_id: UUID | None = None,
    operation_id: UUID | None = None,
    manual_request_id: UUID | None = None,
    key_id: UUID | None = None,
    generation: int | None = None,
    reason: str | None = None,
    mode: str | None = None,
    management_epoch: int | None = None,
    stop_watermark: int | None = None,
    drain_generation: int | None = None,
) -> None:
    """Append known scalar references in the caller's transaction; never accept payload metadata."""
    if action not in ACTIONS:
        raise ValueError("Unregistered synchronization audit action")
    if reason not in {None, "unauthorized", "forbidden", "not_found"} or mode not in {
        None,
        "feed",
        "stopping",
        "manual",
    }:
        raise ValueError("Unregistered synchronization audit state")
    key_id = key_id if key_id is not None else actor.key_id
    metadata: dict = {
        "schema": SCHEMA,
        "correlation_id": str(request.correlation_id) if (request := _request.get()) else None,
        "actor_kind": actor.kind,
        "client_id": actor.client_id,
        "issuer": actor.issuer,
        "mode": mode,
        "management_epoch": management_epoch,
        "stop_watermark": stop_watermark,
        "drain_generation": drain_generation,
        "reason": reason,
    }
    if installation is not None:
        workspace_id, service_id, generation = (
            installation.workspace_id,
            installation.service_id,
            installation.generation,
        )
        metadata.update(
            {
                "installation_id": str(installation.id),
                "resource_pk": str(installation.resource_pk),
                "context_id": str(installation.context_id),
                "endpoint_id": str(installation.endpoint_id),
            }
        )
        resource = "sync:" + str(installation.id)
    elif authorization_id is not None and workspace_id is not None:
        resource = "sync-workspace:" + str(workspace_id)
    elif service_id is not None:
        resource = "sync-service:" + service_id
    else:
        resource = "sync-denial"
    for name, value in {
        "workspace_id": workspace_id,
        "service_id": service_id,
        "authorization_id": authorization_id,
        "feed_id": feed_id,
        "run_id": run_id,
        "operation_id": operation_id,
        "manual_request_id": manual_request_id,
        "key_id": key_id,
        "generation": generation,
    }.items():
        if value is not None:
            metadata[name] = str(value) if isinstance(value, UUID) else value
    db.add(
        AuditLog(
            user_id=actor.subject,
            user_email="system:sync-audit",
            action=action,
            resource=resource,
            user_metadata=metadata,
            created_at=utcnow(),
        )
    )


def event_view(row: AuditLog) -> dict:
    """Project only registered scalars and pseudonymized human identity, never arbitrary metadata."""
    from services.erasure_details import erasure_pseudonym

    data = row.user_metadata or {}
    actor = {
        "kind": data.get("actor_kind"),
        "client_id": data.get("client_id"),
        "issuer": data.get("issuer"),
        "subject_ref": erasure_pseudonym(row.user_id),
    }
    result = {"id": row.id, "action": row.action, "at": to_utc_iso(row.created_at), "actor": actor}
    for name in (
        "workspace_id",
        "service_id",
        "installation_id",
        "resource_pk",
        "context_id",
        "endpoint_id",
        "authorization_id",
        "feed_id",
        "run_id",
        "operation_id",
        "manual_request_id",
        "key_id",
        "generation",
        "reason",
        "correlation_id",
        "mode",
        "management_epoch",
        "stop_watermark",
        "drain_generation",
    ):
        result[name] = data.get(name)
    return result


async def record_denial(status: int) -> None:
    """Best-effort isolated record of already authenticated authority; never log bearer inputs."""
    request = _request.get()
    if (
        status not in {401, 403, 404}
        or request is None
        or request.actor is None
        or request.db is None
    ):
        return
    try:
        async with timeout(1):
            async with isolated_session(request.db) as db:
                if request.actor.kind == "human":
                    if request.actor.account_id is None:
                        return
                    live = await db.scalar(
                        select(User.id)
                        .where(
                            User.id == request.actor.account_id,
                            User.user_id == request.actor.subject,
                        )
                        .with_for_update(read=True, key_share=True, nowait=True)
                    )
                    if live is None:
                        return
                add_audit(
                    db,
                    "sync.authority.denied",
                    request.actor,
                    installation=request.installation,
                    reason={401: "unauthorized", 403: "forbidden", 404: "not_found"}[status],
                )
                await db.commit()
    except Exception:
        logger.warning("sync_denial_audit_unavailable")


async def prune_activity(db: AsyncSession, *, limit: int = 500) -> None:
    """Bound cleanup to this protocol; do not change another audit producer's retention."""
    if not 1 <= limit <= 500:
        raise ValueError("Activity cleanup limit must be between 1 and 500")
    cutoff = utcnow() - timedelta(days=RETENTION_DAYS)
    audit_ids = (
        select(AuditLog.id)
        .where(
            AuditLog.user_metadata["schema"].as_string() == SCHEMA,
            AuditLog.created_at < cutoff,
        )
        .order_by(AuditLog.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    request_ids = (
        select(SyncManualRequest.id)
        .where(
            SyncManualRequest.created_at < cutoff,
        )
        .order_by(SyncManualRequest.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    await db.execute(delete(AuditLog).where(AuditLog.id.in_(audit_ids)))
    await db.execute(delete(SyncManualRequest).where(SyncManualRequest.id.in_(request_ids)))
