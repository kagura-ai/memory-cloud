"""Verified custom domains of public MCP endpoints: states and writes (#2809).

The contract is ``docs/public-mcp-domains.md``. A row binds one host name to
one publication (``public_mcp_endpoints.id``) of one workspace.

States::

    attach ──> pending ──verify: proof ok, right ok, name free──> active
    pending ──7 days, or owner detach──> (row deleted)
    active ──>= 3 confirmed absences and >= 72 h since proof_ok_at──> suspended(proof_lost)
    active ──right not active at a check──> suspended(right_ended)
    active ──name became reserved──> suspended(reserved)
    suspended ──proof ok and right ok and not reserved──> active
    suspended ──30 days since suspended_at, not reinstated at that check──> released
    active | suspended ──detach, unpublish, endpoint or workspace gone, release──> released
    released ──cooldown_until passed──> (row deleted)

Invariants:

- I1 Holder. A row becomes a holder (``active`` / ``suspended``) only by
  ``pending → active``, under the per-name advisory lock, after one statement
  that reads every other holder of the name and every ``released`` row of
  another workspace whose cooldown is still running; any hit refuses. The
  partial unique index on held names is the backstop (its integrity error is
  a 409). Leaving the holding state needs no lock.
- I2 Fence. A lookup result is applied only to the row version it was taken
  from (``check_seq``); every UPDATE increments ``check_seq``. A row that
  changed meanwhile keeps its state (owner: 409; job: counted as fenced).
- I3 No connection during a lookup. The row is read, the transaction ends,
  the DNS lookup runs, then the write transaction starts.
- I4 Lock order. Workspace row (the paid guard) → advisory lock (per
  workspace in attach, per name at activation) → endpoint row (``FOR SHARE``,
  attach only) → domain row. Unpublish writes its endpoint row, then the
  domain rows; it takes no workspace row lock and no advisory lock. Verify
  never locks the endpoint row.
- I5 Proof only decides proof. An ``unavailable`` lookup never changes
  ``state``, ``proof_ok_at`` or ``proof_missing_checks``; it records the check
  and, under an active right, renews the lease. A right that ended suspends
  whatever the lookup said.
- I6 A dead token stays dead. A token belongs to one row; attach always mints
  a new one.
- I7 A lease, always. No row is ``active`` without ``serve_until``; nothing is
  served past it; it is extended only by a completed check under an active
  right.

The database clock decides every time: no entry point takes a time argument
or reads the application clock. The writer reads the database clock once in
its transaction and passes it to ``decide_check`` as ``db_now``.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import and_, delete, func, literal_column, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from db.redis import get_redis_client, incrby_counter
from mcp_server.public.domains import claimable_hostname
from models.auth import AuditLog, User, Workspace
from models.public_mcp import PublicMcpEndpoint
from models.public_mcp_domain import (
    ACTIVATIONS_PER_DAY,
    CHECK_TIMEOUT_SECONDS,
    DOMAIN_SERVICE_ID,
    HELD_STATES,
    PROOF_GRACE_HOURS,
    PROOF_MIN_MISSING_CHECKS,
    SERVE_LEASE_HOURS,
    SUSPENDED_HOLD_DAYS,
    VERIFY_LIMIT_PER_HOUR,
    PublicMcpDomain,
)
from services import public_mcp_domain_dns as domain_dns
from services.integration_entitlements import paid_integration_write
from services.public_mcp_domain_dns import DomainCheck, ProofResult, RoutingResult
from utils.exceptions import (
    AuthorizationError,
    MemoryCloudException,
    NotFoundException,
    RateLimitError,
)
from utils.logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "CLI_ACTOR",
    "SYSTEM_ACTOR",
    "AppliedCheck",
    "AuditAction",
    "Decision",
    "DomainActor",
    "DomainSnapshot",
    "DomainWriteAccess",
    "PublicMcpDomainChangedError",
    "PublicMcpDomainExistsError",
    "PublicMcpDomainHeldError",
    "PublicMcpDomainHostnameNotAllowedError",
    "PublicMcpDomainInCooldownError",
    "PublicMcpDomainLimitReachedError",
    "PublicMcpDomainNotEntitledError",
    "PublicMcpDomainOwnerRequiredError",
    "PublicMcpDomainRateLimitError",
    "PublicMcpDomainVerificationUnavailableError",
    "PublicMcpEndpointNotPublishedError",
    "apply_check",
    "attach_domain",
    "decide_check",
    "detach_domain",
    "domain_write",
    "list_domains",
    "lookup",
    "owner_actor",
    "release_domains_of_endpoint",
    "verify_domain",
]

DomainState = Literal["pending", "active", "suspended", "released"]
SuspendedReason = Literal["proof_lost", "right_ended", "reserved"]
AuditAction = Literal["attached", "verified", "suspended", "reinstated", "detached", "released"]

# The database clock, the only clock of every decision here.
_DB_NOW = literal_column("timezone('UTC', statement_timestamp())")
# Owner action windows of the two Redis counters (seconds).
VERIFY_WINDOW_SECONDS = 3600
ACTIVATION_WINDOW_SECONDS = 86400
OPEN_STATES: tuple[str, ...] = ("pending", "active", "suspended")

_UNAVAILABLE = DomainCheck("unavailable", "unresolved")


# ----------------------------------------------------------------------------
# The proof lookup (R6: the time bound lives with the caller)
# ----------------------------------------------------------------------------


async def lookup(hostname: str, token: str) -> DomainCheck:
    """Check the proof and the routing of ``hostname``, bounded in time.

    A timeout, or any exception of the resolver (a replaced one included), is
    ``DomainCheck("unavailable", "unresolved")``. Nothing is logged.
    """
    try:
        resolver = domain_dns.domain_resolver()
        return await asyncio.wait_for(
            resolver.check(hostname, token, get_settings().mcp_public_domains_routing_target),
            CHECK_TIMEOUT_SECONDS,
        )
    except Exception:
        return _UNAVAILABLE


# ----------------------------------------------------------------------------
# The pure decision of one check (R7)
# ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DomainSnapshot:
    """The fields of a held row that a check decides on."""

    state: str
    suspended_reason: str | None
    proof_ok_at: datetime | None
    proof_missing_checks: int
    verified_at: datetime | None
    suspended_at: datetime | None


@dataclass(frozen=True, slots=True)
class Decision:
    """What one check writes to a held row.

    ``renew_lease`` asks the writer to extend ``serve_until``; a ``released``
    state asks it to write ``released_at`` and ``cooldown_until``.
    """

    state: str
    suspended_reason: str | None
    suspended_at: datetime | None
    proof_ok_at: datetime | None
    proof_missing_checks: int
    last_checked_at: datetime
    last_proof_result: ProofResult
    routing_status: RoutingResult
    renew_lease: bool
    audit_action: AuditAction | None


def decide_check(
    snapshot: DomainSnapshot,
    proof: ProofResult,
    routing: RoutingResult,
    right_active: bool,
    reserved: bool,
    db_now: datetime,
    /,
) -> Decision:
    """Decide what one completed check does to an ``active`` or ``suspended`` row.

    Rules, in this order:

    1. The check is recorded (time, proof result, routing status).
    2. ``ok``: ``proof_ok_at = db_now``, no absences. ``missing``: one more
       confirmed absence. ``unavailable``: neither changes.
    3. ``active``: reserved → ``suspended(reserved)``; else right not active
       → ``suspended(right_ended)``; else a ``missing`` proof with at least
       ``PROOF_MIN_MISSING_CHECKS`` absences and ``PROOF_GRACE_HOURS`` since
       the proof was last seen → ``suspended(proof_lost)``; else it stays
       active and the lease is renewed.
    4. ``suspended``: reserved → reason ``reserved``; else right not active →
       ``right_ended``; else ``ok`` → ``active`` (lease renewed); else
       ``missing`` → ``proof_lost``; else unchanged. Still suspended and
       ``SUSPENDED_HOLD_DAYS`` since ``suspended_at`` → ``released``. A change
       of reason alone keeps ``suspended_at`` and is not audited.

    Args:
        snapshot: The row as read under the fence.
        proof: The proof result of the lookup.
        routing: The routing status of the lookup.
        right_active: Whether the right was active in the write transaction.
        reserved: Whether the name is reserved now.
        db_now: The database clock of the write transaction.

    Returns:
        The values to write.
    """
    proof_ok_at = snapshot.proof_ok_at
    missing_checks = snapshot.proof_missing_checks
    if proof == "ok":
        proof_ok_at, missing_checks = db_now, 0
    elif proof == "missing":
        missing_checks += 1

    state = snapshot.state
    reason = snapshot.suspended_reason
    suspended_at = snapshot.suspended_at
    renew = False
    action: AuditAction | None = None

    if state == "active":
        new_reason: str | None = None
        if reserved:
            new_reason = "reserved"
        elif not right_active:
            new_reason = "right_ended"
        elif proof == "missing" and missing_checks >= PROOF_MIN_MISSING_CHECKS:
            seen = snapshot.proof_ok_at or snapshot.verified_at
            if seen is not None and db_now - seen >= timedelta(hours=PROOF_GRACE_HOURS):
                new_reason = "proof_lost"
        if new_reason is None:
            renew = True
        else:
            state, reason, suspended_at, action = "suspended", new_reason, db_now, "suspended"
    elif state == "suspended":
        if reserved:
            reason = "reserved"
        elif not right_active:
            reason = "right_ended"
        elif proof == "ok":
            state, reason, suspended_at, renew, action = "active", None, None, True, "reinstated"
        elif proof == "missing":
            reason = "proof_lost"
        if (
            state == "suspended"
            and suspended_at is not None
            and db_now - suspended_at >= timedelta(days=SUSPENDED_HOLD_DAYS)
        ):
            state, reason, action = "released", None, "released"

    return Decision(
        state=state,
        suspended_reason=reason,
        suspended_at=suspended_at,
        proof_ok_at=proof_ok_at,
        proof_missing_checks=missing_checks,
        last_checked_at=db_now,
        last_proof_result=proof,
        routing_status=routing,
        renew_lease=renew,
        audit_action=action,
    )


# ----------------------------------------------------------------------------
# Errors (R9); none names another workspace
# ----------------------------------------------------------------------------


class _DomainError(MemoryCloudException):
    """An expected refusal of a custom domain action (logged as a warning)."""

    log_level = "warning"

    def __init__(self, message: str, *, status_code: int, error_code: str, **details: object):
        super().__init__(message, status_code=status_code, error_code=error_code, **details)


class PublicMcpDomainOwnerRequiredError(_DomainError):
    """Attach and verify need the workspace's canonical owner (403)."""

    def __init__(self) -> None:
        super().__init__(
            "Only the workspace owner can add or verify a custom domain.",
            status_code=403,
            error_code="public_mcp_domain_owner_required",
        )


class PublicMcpDomainNotEntitledError(_DomainError):
    """The workspace has no active right to custom domains (403)."""

    def __init__(self) -> None:
        super().__init__(
            "Custom domains are not available for this workspace.",
            status_code=403,
            error_code="public_mcp_domain_not_entitled",
        )


class PublicMcpDomainHostnameNotAllowedError(_DomainError):
    """The host name is malformed or reserved: one code for both (422)."""

    def __init__(self) -> None:
        super().__init__(
            "This host name cannot be used as a custom domain.",
            status_code=422,
            error_code="public_mcp_domain_hostname_not_allowed",
            field="hostname",
        )


class PublicMcpDomainLimitReachedError(_DomainError):
    """The workspace holds its maximum number of domains (409)."""

    def __init__(self) -> None:
        super().__init__(
            "The workspace has reached its number of custom domains.",
            status_code=409,
            error_code="public_mcp_domain_limit_reached",
        )


class PublicMcpDomainExistsError(_DomainError):
    """The publication already has a domain (409)."""

    def __init__(self) -> None:
        super().__init__(
            "This public MCP endpoint already has a custom domain.",
            status_code=409,
            error_code="public_mcp_domain_exists",
        )


class PublicMcpEndpointNotPublishedError(_DomainError):
    """The publication changed while the domain was verified (409)."""

    def __init__(self) -> None:
        super().__init__(
            "The public MCP endpoint of this domain is no longer published.",
            status_code=409,
            error_code="public_mcp_endpoint_not_published",
        )


class PublicMcpDomainHeldError(_DomainError):
    """The name is held by another domain (409, only after a valid proof)."""

    def __init__(self) -> None:
        super().__init__(
            "This host name is already in use as a custom domain.",
            status_code=409,
            error_code="public_mcp_domain_held",
        )


class PublicMcpDomainInCooldownError(_DomainError):
    """The name was released recently (409, only after a valid proof)."""

    def __init__(self, available_after: datetime) -> None:
        super().__init__(
            "This host name was released recently and cannot be added yet.",
            status_code=409,
            error_code="public_mcp_domain_in_cooldown",
            available_after=_iso(available_after),
        )


class PublicMcpDomainChangedError(_DomainError):
    """The row changed while it was checked; the caller retries (409)."""

    def __init__(self) -> None:
        super().__init__(
            "The custom domain changed during the check; retry.",
            status_code=409,
            error_code="public_mcp_domain_changed",
        )


class PublicMcpDomainVerificationUnavailableError(_DomainError):
    """The DNS check was indeterminate, or a counter could not be read (503)."""

    def __init__(self) -> None:
        super().__init__(
            "The custom domain could not be checked right now; retry later.",
            status_code=503,
            error_code="public_mcp_domain_verification_unavailable",
        )


class PublicMcpDomainRateLimitError(RateLimitError):
    """Too many verify calls or activation attempts of a workspace (429).

    ``details.limit`` names the limit (``verify_per_hour`` or
    ``activations_per_day``); the ``Retry-After`` header carries the wait.
    """

    log_level = "warning"

    def __init__(self, *, retry_after: int, limit: str) -> None:
        super().__init__("Too many custom domain requests; retry later.", retry_after=retry_after)
        self.details["limit"] = limit
        self.response_headers = {"Retry-After": str(retry_after)}


def _iso(value: datetime) -> str:
    return value.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


# ----------------------------------------------------------------------------
# Actors and audit (R9)
# ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DomainActor:
    """Who caused a transition: the audit row's ``user_id`` and ``user_email``."""

    user_id: str
    email: str


SYSTEM_ACTOR = DomainActor("system:public-mcp-domains", "system:public-mcp-domains")
CLI_ACTOR = DomainActor("operator:cli", "operator:cli")


async def owner_actor(db: AsyncSession, user_id: str) -> DomainActor:
    """The actor of an owner action: the user id and the e-mail of its users row."""
    email = await db.scalar(select(User.email).where(User.user_id == user_id))
    return DomainActor(user_id, email or user_id)


def hostname_digest(hostname: str) -> str:
    """The SHA-256 of a host name: what audit rows keep instead of the name."""
    return hashlib.sha256(hostname.encode()).hexdigest()


def _audit(
    db: AsyncSession,
    actor: DomainActor,
    action: AuditAction,
    *,
    domain_id: UUID,
    workspace_id: UUID,
    endpoint_id: UUID,
    hostname: str,
    from_state: str | None,
    to_state: str | None,
    reason: str | None = None,
) -> None:
    """Add one audit row of a transition to the transition's transaction, and log it."""
    db.add(
        AuditLog(
            user_id=actor.user_id,
            user_email=actor.email,
            action=f"public_mcp_domain.{action}",
            resource=f"public_mcp_domain:{domain_id}",
            user_metadata={
                "workspace_id": str(workspace_id),
                "endpoint_id": str(endpoint_id),
                "from_state": from_state,
                "to_state": to_state,
                "reason": reason,
                "hostname_sha256": hostname_digest(hostname),
            },
        )
    )
    logger.info(
        f"public_mcp_domain_{action}",
        domain_id=str(domain_id),
        workspace_id=str(workspace_id),
        endpoint_id=str(endpoint_id),
        from_state=from_state,
        to_state=to_state,
        reason=reason,
    )


# ----------------------------------------------------------------------------
# The write transaction (R7, R8): the only code that names the right
# ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DomainWriteAccess:
    """The right a write transaction runs under: ``valid_until`` bounds the lease."""

    valid_until: datetime | None


@asynccontextmanager
async def domain_write(db: AsyncSession, workspace_id: UUID) -> AsyncIterator[DomainWriteAccess]:
    """Own one write transaction of a domain change, under the right.

    The caller's transaction is committed first (plain reads only may precede
    it). Under ``MCP_PUBLIC_DOMAINS_AUTHORITY=entitlement`` the transaction
    runs inside ``paid_integration_write`` for the license
    ``DOMAIN_SERVICE_ID`` (workspace row lock first, right re-checked before
    the guard commits); a refusal at entry or at the re-check is raised as
    ``PublicMcpDomainNotEntitledError`` after the rollback. Under
    ``operator`` every canonical owner has the right: an ordinary transaction
    that this helper commits, ``valid_until = None``. The body never commits.
    """
    await db.commit()
    if get_settings().mcp_public_domains_authority == "operator":
        try:
            yield DomainWriteAccess(None)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        return
    in_body = False
    try:
        async with paid_integration_write(db, workspace_id, DOMAIN_SERVICE_ID) as access:
            if access.valid_until is None:
                raise AuthorizationError("Paid integration is not available")
            in_body = True
            yield DomainWriteAccess(access.valid_until)
            in_body = False
    except AuthorizationError as exc:
        if in_body:
            raise
        raise PublicMcpDomainNotEntitledError() from exc


def _lease(access: DomainWriteAccess | None, db_now: datetime) -> datetime:
    """``db_now + SERVE_LEASE_HOURS``, never past the published right's end."""
    lease = db_now + timedelta(hours=SERVE_LEASE_HOURS)
    if access is not None and access.valid_until is not None:
        end = access.valid_until
        if end.tzinfo is not None:
            end = end.astimezone(UTC).replace(tzinfo=None)
        lease = min(lease, end)
    return lease


async def _db_now(db: AsyncSession) -> datetime:
    now = await db.scalar(select(_DB_NOW))
    if not isinstance(now, datetime):  # pragma: no cover - the database returns a timestamp
        raise RuntimeError("database clock unavailable")
    return now


def _cooldown() -> timedelta:
    return timedelta(hours=get_settings().mcp_public_domains_reassign_cooldown_hours)


# ----------------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------------


async def _is_canonical_owner(db: AsyncSession, user_id: str, workspace_id: UUID) -> bool:
    owner = await db.scalar(select(Workspace.owner_user_id).where(Workspace.id == workspace_id))
    return owner is not None and owner == user_id


async def _live_endpoint(
    db: AsyncSession, workspace_id: UUID, resource_id: str, *, share: bool = False
) -> PublicMcpEndpoint | None:
    """The live (not revoked) publication of ``(workspace, resource slug)``."""
    stmt = select(PublicMcpEndpoint).where(
        PublicMcpEndpoint.workspace_id == workspace_id,
        PublicMcpEndpoint.resource_id == resource_id,
        PublicMcpEndpoint.revoked_at.is_(None),
    )
    if share:
        stmt = stmt.with_for_update(read=True)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()


async def _require_live_endpoint(
    db: AsyncSession, workspace_id: UUID, resource_id: str
) -> PublicMcpEndpoint:
    endpoint = await _live_endpoint(db, workspace_id, resource_id)
    if endpoint is None:
        raise NotFoundException("Public MCP endpoint")
    return endpoint


async def _open_row(
    db: AsyncSession, workspace_id: UUID, endpoint_id: UUID, domain_id: UUID, *, lock: bool
) -> PublicMcpDomain | None:
    """A row of this workspace and publication that is not ``released``."""
    stmt = select(PublicMcpDomain).where(
        PublicMcpDomain.id == domain_id,
        PublicMcpDomain.workspace_id == workspace_id,
        PublicMcpDomain.endpoint_id == endpoint_id,
        PublicMcpDomain.state != "released",
    )
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()


async def _fenced_row(db: AsyncSession, domain_id: UUID, seen_seq: int) -> PublicMcpDomain | None:
    """The row ``FOR UPDATE`` if it is still the version a lookup was taken from (I2)."""
    return (
        await db.execute(
            select(PublicMcpDomain)
            .where(PublicMcpDomain.id == domain_id, PublicMcpDomain.check_seq == seen_seq)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def reload_domain(db: AsyncSession, domain_id: UUID) -> PublicMcpDomain | None:
    """The row as it is now (a fresh read)."""
    return (
        await db.execute(
            select(PublicMcpDomain)
            .where(PublicMcpDomain.id == domain_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def list_domains(
    db: AsyncSession, workspace_id: UUID, resource_id: str
) -> Sequence[PublicMcpDomain]:
    """The rows of the live publication of ``resource_id`` that are not released."""
    endpoint = await _require_live_endpoint(db, workspace_id, resource_id)
    return (
        (
            await db.execute(
                select(PublicMcpDomain)
                .where(
                    PublicMcpDomain.workspace_id == workspace_id,
                    PublicMcpDomain.endpoint_id == endpoint.id,
                    PublicMcpDomain.state != "released",
                )
                .order_by(PublicMcpDomain.created_at)
            )
        )
        .scalars()
        .all()
    )


# ----------------------------------------------------------------------------
# Owner action counters (R9): Redis, fail closed
# ----------------------------------------------------------------------------


async def _remaining_ttl(key: str) -> int | None:
    try:
        remaining = await get_redis_client().ttl(key)
    except Exception:
        return None
    return int(remaining) if isinstance(remaining, int) and remaining > 0 else None


async def _count_owner_action(
    key: str, *, window_seconds: int, limit: int, limit_name: str
) -> None:
    """Count one owner action; over ``limit`` in the window is a 429.

    A Redis error refuses the action (503): the counter bounds outbound
    lookups and certificate orders, so it never fails open.
    """
    try:
        count = await incrby_counter(key, 1, ttl=window_seconds)
    except Exception as exc:
        logger.warning("public_mcp_domain_counter_unavailable", error_type=type(exc).__name__)
        raise PublicMcpDomainVerificationUnavailableError() from exc
    if count > limit:
        retry_after = await _remaining_ttl(key) or window_seconds
        raise PublicMcpDomainRateLimitError(retry_after=retry_after, limit=limit_name)


async def _count_verify(workspace_id: UUID) -> None:
    await _count_owner_action(
        f"public_mcp_domain_verify:{workspace_id}",
        window_seconds=VERIFY_WINDOW_SECONDS,
        limit=VERIFY_LIMIT_PER_HOUR,
        limit_name="verify_per_hour",
    )


async def _count_activation(workspace_id: UUID) -> None:
    await _count_owner_action(
        f"public_mcp_domain_activate:{workspace_id}",
        window_seconds=ACTIVATION_WINDOW_SECONDS,
        limit=ACTIVATIONS_PER_DAY,
        limit_name="activations_per_day",
    )


# ----------------------------------------------------------------------------
# Attach (R7)
# ----------------------------------------------------------------------------


async def _lock(db: AsyncSession, key: str) -> None:
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})


async def attach_domain(
    db: AsyncSession, user_id: str, workspace_id: UUID, resource_id: str, raw_hostname: str
) -> PublicMcpDomain:
    """Claim ``raw_hostname`` for the live publication of ``resource_id`` (``pending``).

    The claim holds nothing and reads no other workspace's rows; every attach
    mints a new token.

    Raises:
        PublicMcpDomainOwnerRequiredError: The caller is not the canonical owner.
        PublicMcpDomainHostnameNotAllowedError: Malformed or reserved name.
        NotFoundException: No live publication of the resource.
        PublicMcpDomainNotEntitledError: No active right.
        PublicMcpDomainLimitReachedError: The workspace limit is reached.
        PublicMcpDomainExistsError: The publication already has a domain.
    """
    if not await _is_canonical_owner(db, user_id, workspace_id):
        raise PublicMcpDomainOwnerRequiredError()
    hostname = claimable_hostname(raw_hostname)
    if hostname is None:
        raise PublicMcpDomainHostnameNotAllowedError()
    await _require_live_endpoint(db, workspace_id, resource_id)
    actor = await owner_actor(db, user_id)
    async with domain_write(db, workspace_id):
        if not await _is_canonical_owner(db, user_id, workspace_id):
            raise PublicMcpDomainOwnerRequiredError()
        await _lock(db, f"public_mcp_domain_ws:{workspace_id}")
        # The publication as it is now, held against a concurrent unpublish:
        # its id is the one the row binds, never the one read before.
        endpoint = await _live_endpoint(db, workspace_id, resource_id, share=True)
        if endpoint is None:
            raise NotFoundException("Public MCP endpoint")
        open_rows = await db.scalar(
            select(func.count())
            .select_from(PublicMcpDomain)
            .where(
                PublicMcpDomain.workspace_id == workspace_id,
                PublicMcpDomain.state.in_(OPEN_STATES),
            )
        )
        if (open_rows or 0) >= get_settings().mcp_public_domains_max_per_workspace:
            raise PublicMcpDomainLimitReachedError()
        existing = await db.scalar(
            select(PublicMcpDomain.id).where(
                PublicMcpDomain.endpoint_id == endpoint.id,
                PublicMcpDomain.state != "released",
            )
        )
        if existing is not None:
            raise PublicMcpDomainExistsError()
        row = PublicMcpDomain(
            hostname=hostname,
            workspace_id=workspace_id,
            endpoint_id=endpoint.id,
            state="pending",
            verification_token=secrets.token_hex(32),
        )
        db.add(row)
        try:
            await db.flush()
        except IntegrityError as exc:
            raise PublicMcpDomainExistsError() from exc
        # The server defaults (id, created_at), read in the same transaction:
        # the caller gets the row as committed, even if an unpublish deletes
        # it right after.
        await db.refresh(row)
        _audit(
            db,
            actor,
            "attached",
            domain_id=row.id,
            workspace_id=workspace_id,
            endpoint_id=endpoint.id,
            hostname=hostname,
            from_state=None,
            to_state="pending",
        )
    return row


# ----------------------------------------------------------------------------
# The one writer of a check (R7)
# ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AppliedCheck:
    """What ``apply_check`` did.

    ``outcome`` is ``fenced`` when the row changed during the lookup (nothing
    written). ``right_active`` is false when the write ran without the right
    (an ``active`` row is then suspended).
    """

    outcome: Literal["applied", "fenced"]
    right_active: bool
    from_state: str | None = None
    to_state: str | None = None
    audit_action: AuditAction | None = None
    suspended_reason: str | None = None


def _snapshot(row: PublicMcpDomain) -> DomainSnapshot:
    return DomainSnapshot(
        state=row.state,
        suspended_reason=row.suspended_reason,
        proof_ok_at=row.proof_ok_at,
        proof_missing_checks=row.proof_missing_checks,
        verified_at=row.verified_at,
        suspended_at=row.suspended_at,
    )


async def _apply(
    db: AsyncSession,
    domain_id: UUID,
    seen_seq: int,
    check: DomainCheck,
    actor: DomainActor,
    access: DomainWriteAccess | None,
) -> AppliedCheck:
    right_active = access is not None
    db_now = await _db_now(db)
    row = await _fenced_row(db, domain_id, seen_seq)
    if row is None or row.state not in HELD_STATES:
        return AppliedCheck("fenced", right_active)
    reserved = claimable_hostname(row.hostname) is None
    decision = decide_check(
        _snapshot(row), check.proof, check.routing, right_active, reserved, db_now
    )
    from_state = row.state
    row.state = decision.state
    row.suspended_reason = decision.suspended_reason
    row.suspended_at = decision.suspended_at
    row.proof_ok_at = decision.proof_ok_at
    row.proof_missing_checks = decision.proof_missing_checks
    row.last_checked_at = decision.last_checked_at
    row.last_proof_result = decision.last_proof_result
    row.routing_status = decision.routing_status
    if decision.renew_lease:
        row.serve_until = _lease(access, db_now)
    if decision.state == "released":
        row.released_at = db_now
        row.cooldown_until = db_now + _cooldown()
    row.check_seq = row.check_seq + 1
    await db.flush()
    if decision.audit_action is not None:
        _audit(
            db,
            actor,
            decision.audit_action,
            domain_id=row.id,
            workspace_id=row.workspace_id,
            endpoint_id=row.endpoint_id,
            hostname=row.hostname,
            from_state=from_state,
            to_state=decision.state,
            reason=decision.suspended_reason,
        )
    return AppliedCheck(
        "applied",
        right_active,
        from_state,
        decision.state,
        decision.audit_action,
        decision.suspended_reason,
    )


async def apply_check(
    db: AsyncSession,
    domain_id: UUID,
    seen_seq: int,
    check: DomainCheck,
    actor: DomainActor,
) -> AppliedCheck:
    """Apply one completed check to an ``active`` / ``suspended`` row.

    Used by the owner's verify and by the maintenance job. Under the right
    (``domain_write``): read the database clock, re-read the row with the
    fence, decide with ``right_active=True`` and write. When the right is
    refused, a second, ordinary transaction applies the same check with
    ``right_active=False`` (an ``active`` row becomes
    ``suspended(right_ended)``; a suspended row keeps its hold).

    Args:
        db: Session; its transaction is committed first (plain reads only).
        domain_id: The row.
        seen_seq: ``check_seq`` of the row when the lookup started.
        check: The completed lookup (``unavailable`` included).
        actor: Who caused the check (audit row of a transition).

    Returns:
        What was done; ``fenced`` when the row changed during the lookup.
    """
    workspace_id = await db.scalar(
        select(PublicMcpDomain.workspace_id).where(PublicMcpDomain.id == domain_id)
    )
    if workspace_id is None:
        await db.commit()
        return AppliedCheck("fenced", False)
    try:
        async with domain_write(db, workspace_id) as access:
            applied = await _apply(db, domain_id, seen_seq, check, actor, access)
        return applied
    except PublicMcpDomainNotEntitledError:
        pass
    try:
        applied = await _apply(db, domain_id, seen_seq, check, actor, None)
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return applied


# ----------------------------------------------------------------------------
# Verify (R7)
# ----------------------------------------------------------------------------


async def _refuse_other_holder(db: AsyncSession, row: PublicMcpDomain, db_now: datetime) -> None:
    """I1: one statement over every other holder and every running cooldown of the name."""
    others = (
        await db.execute(
            select(PublicMcpDomain.state, PublicMcpDomain.cooldown_until).where(
                PublicMcpDomain.hostname == row.hostname,
                PublicMcpDomain.id != row.id,
                or_(
                    PublicMcpDomain.state.in_(HELD_STATES),
                    and_(
                        PublicMcpDomain.state == "released",
                        PublicMcpDomain.workspace_id != row.workspace_id,
                        PublicMcpDomain.cooldown_until > db_now,
                    ),
                ),
            )
        )
    ).all()
    if any(state in HELD_STATES for state, _ in others):
        raise PublicMcpDomainHeldError()
    cooldowns = [until for state, until in others if state == "released" and until is not None]
    if cooldowns:
        raise PublicMcpDomainInCooldownError(max(cooldowns))


async def _activate(
    db: AsyncSession,
    user_id: str,
    workspace_id: UUID,
    resource_id: str,
    domain_id: UUID,
    seen_seq: int,
    check: DomainCheck,
    actor: DomainActor,
) -> None:
    """``pending → active`` after the caller's own proof was found (I1)."""
    await _count_activation(workspace_id)
    async with domain_write(db, workspace_id) as access:
        if not await _is_canonical_owner(db, user_id, workspace_id):
            raise PublicMcpDomainOwnerRequiredError()
        row_hostname = await db.scalar(
            select(PublicMcpDomain.hostname).where(PublicMcpDomain.id == domain_id)
        )
        if row_hostname is None:
            raise PublicMcpDomainChangedError()
        await _lock(db, f"public_mcp_domain:{row_hostname}")
        db_now = await _db_now(db)
        row = await _fenced_row(db, domain_id, seen_seq)
        if row is None or row.state != "pending":
            raise PublicMcpDomainChangedError()
        # A plain read: verify never locks the endpoint row (I4).
        endpoint = await _live_endpoint(db, workspace_id, resource_id)
        if endpoint is None or endpoint.id != row.endpoint_id:
            raise PublicMcpEndpointNotPublishedError()
        if claimable_hostname(row.hostname) is None:
            raise PublicMcpDomainHostnameNotAllowedError()
        await _refuse_other_holder(db, row, db_now)
        row.state = "active"
        row.verified_at = db_now
        row.proof_ok_at = db_now
        row.proof_missing_checks = 0
        row.serve_until = _lease(access, db_now)
        row.last_checked_at = db_now
        row.last_proof_result = check.proof
        row.routing_status = check.routing
        row.check_seq = row.check_seq + 1
        try:
            await db.flush()
        except IntegrityError as exc:
            raise PublicMcpDomainHeldError() from exc
        _audit(
            db,
            actor,
            "verified",
            domain_id=row.id,
            workspace_id=workspace_id,
            endpoint_id=row.endpoint_id,
            hostname=row.hostname,
            from_state="pending",
            to_state="active",
        )


async def _record_pending_check(
    db: AsyncSession, domain_id: UUID, seen_seq: int, check: DomainCheck
) -> None:
    """A ``pending`` row whose proof was not found: record the check (fenced)."""
    try:
        result = await db.execute(
            update(PublicMcpDomain)
            .where(
                PublicMcpDomain.id == domain_id,
                PublicMcpDomain.check_seq == seen_seq,
                PublicMcpDomain.state == "pending",
            )
            .values(
                last_checked_at=_DB_NOW,
                last_proof_result=check.proof,
                routing_status=check.routing,
                check_seq=PublicMcpDomain.check_seq + 1,
            )
            .execution_options(synchronize_session=False)
        )
        changed = getattr(result, "rowcount", 0)
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    if changed != 1:
        raise PublicMcpDomainChangedError()


async def verify_domain(
    db: AsyncSession, user_id: str, workspace_id: UUID, resource_id: str, domain_id: UUID
) -> PublicMcpDomain:
    """Check the proof of a domain now and apply the result.

    ``pending``: activation when the proof is found, the right is active and
    the name is free (held / cooldown are answered only then).
    ``active`` / ``suspended``: the same check the job runs (``apply_check``).

    Raises:
        PublicMcpDomainOwnerRequiredError: Not the canonical owner (before the
            counter and before any lookup).
        NotFoundException: No such open row of this workspace and publication.
        PublicMcpDomainRateLimitError: Over a counter's limit.
        PublicMcpDomainVerificationUnavailableError: Indeterminate lookup, or
            a counter could not be read; nothing is written.
        PublicMcpDomainNotEntitledError: No active right (an ``active`` row is
            suspended first).
        PublicMcpDomainChangedError: The row changed during the lookup.
    """
    if not await _is_canonical_owner(db, user_id, workspace_id):
        raise PublicMcpDomainOwnerRequiredError()
    endpoint = await _require_live_endpoint(db, workspace_id, resource_id)
    row = await _open_row(db, workspace_id, endpoint.id, domain_id, lock=False)
    if row is None:
        raise NotFoundException("Public MCP domain")
    await _count_verify(workspace_id)
    actor = await owner_actor(db, user_id)
    state, seen_seq = row.state, row.check_seq
    hostname, token = row.hostname, row.verification_token
    await db.commit()  # I3: no connection is held during the lookup
    check = await lookup(hostname, token)
    if check.proof == "unavailable":
        raise PublicMcpDomainVerificationUnavailableError()
    if state == "pending":
        if check.proof == "ok":
            await _activate(
                db, user_id, workspace_id, resource_id, domain_id, seen_seq, check, actor
            )
        else:
            await _record_pending_check(db, domain_id, seen_seq, check)
    else:
        applied = await apply_check(db, domain_id, seen_seq, check, actor)
        if applied.outcome == "fenced":
            raise PublicMcpDomainChangedError()
        if not applied.right_active:
            raise PublicMcpDomainNotEntitledError()
    current = await reload_domain(db, domain_id)
    if current is None or current.state == "released":
        raise NotFoundException("Public MCP domain")
    return current


# ----------------------------------------------------------------------------
# Detach, unpublish (R7)
# ----------------------------------------------------------------------------


def _release(row: PublicMcpDomain, db_now: datetime) -> None:
    """Mark a held row ``released`` with the cooldown (no lock needed, I1)."""
    row.state = "released"
    row.suspended_reason = None
    row.released_at = db_now
    row.cooldown_until = db_now + _cooldown()
    row.check_seq = row.check_seq + 1


async def detach_domain(
    db: AsyncSession, user_id: str, workspace_id: UUID, resource_id: str, domain_id: UUID
) -> None:
    """Remove a domain of the publication; never needs the right.

    ``pending``: the row is deleted. ``active`` / ``suspended``: ``released``
    with the cooldown. Audited, committed here.
    """
    endpoint = await _require_live_endpoint(db, workspace_id, resource_id)
    actor = await owner_actor(db, user_id)
    try:
        row = await _open_row(db, workspace_id, endpoint.id, domain_id, lock=True)
        if row is None:
            raise NotFoundException("Public MCP domain")
        from_state = row.state
        if from_state == "pending":
            await db.delete(row)
            to_state = None
        else:
            _release(row, await _db_now(db))
            to_state = "released"
        await db.flush()
        _audit(
            db,
            actor,
            "detached",
            domain_id=domain_id,
            workspace_id=workspace_id,
            endpoint_id=endpoint.id,
            hostname=row.hostname,
            from_state=from_state,
            to_state=to_state,
        )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


async def release_domains_of_endpoint(
    db: AsyncSession, endpoint_id: UUID, actor: DomainActor
) -> int:
    """Release every domain of an unpublished publication, in the caller's transaction.

    First the ``pending`` rows are deleted, then the holders are released with
    the cooldown: a row activated while the first statement waited is caught
    by the second. Takes no workspace row lock and no advisory lock (I4); the
    caller commits.

    Returns:
        The number of rows deleted or released.
    """
    removed = (
        await db.execute(
            delete(PublicMcpDomain)
            .where(PublicMcpDomain.endpoint_id == endpoint_id, PublicMcpDomain.state == "pending")
            .returning(PublicMcpDomain.id, PublicMcpDomain.workspace_id, PublicMcpDomain.hostname)
            .execution_options(synchronize_session=False)
        )
    ).all()
    for domain_id, workspace_id, hostname in removed:
        _audit(
            db,
            actor,
            "released",
            domain_id=domain_id,
            workspace_id=workspace_id,
            endpoint_id=endpoint_id,
            hostname=hostname,
            from_state="pending",
            to_state=None,
            reason="unpublished",
        )
    held = (
        (
            await db.execute(
                select(PublicMcpDomain)
                .where(
                    PublicMcpDomain.endpoint_id == endpoint_id,
                    PublicMcpDomain.state.in_(HELD_STATES),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    if held:
        db_now = await _db_now(db)
        for row in held:
            from_state = row.state
            _release(row, db_now)
            _audit(
                db,
                actor,
                "released",
                domain_id=row.id,
                workspace_id=row.workspace_id,
                endpoint_id=endpoint_id,
                hostname=row.hostname,
                from_state=from_state,
                to_state="released",
                reason="unpublished",
            )
        await db.flush()
    return len(removed) + len(held)
