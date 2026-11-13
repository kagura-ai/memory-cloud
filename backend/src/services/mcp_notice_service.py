"""Service notices over MCP (Issue #2762): consent, notices, delivery.

A deployment's administrators can put a short plain-text notice (a
maintenance window, a change that needs action) in front of the people who
asked for it, at the start of their next AI session:

- ``MCP_NOTICES_ENABLED`` (default off) decides whether the deployment offers
  notices. While it is off nobody is asked, nobody can opt in and nothing is
  delivered (``bootstrap`` runs no notice query). Turning the setting OFF in
  account settings works either way; the admin routes work either way, so a
  notice can be prepared before delivery starts.
- Each person decides for themselves (off by default). Every answer is a row
  in ``mcp_notice_consents`` (append-only, newest row wins), the #2761 shape in
  a table of its own. Bumping :data:`MCP_NOTICES_WORDING_VERSION` does not
  withdraw earlier answers: a broader purpose needs new consent, not a copy
  change.
- A notice is written by whoever passes ``require_admin`` (a system admin in
  ``session`` mode, an operator console in ``service`` mode). Its text passes
  ``services.mcp_notice_text.normalize_notice_text`` and is immutable; every
  create and withdraw writes an ``audit_logs`` row in the same transaction
  (the text's sha256, never the text). Caps: :data:`MCP_NOTICE_MAX_ACTIVE`
  live or scheduled, :data:`MCP_NOTICE_MAX_DAYS` long, scheduled at most
  :data:`MCP_NOTICE_MAX_SCHEDULE_DAYS` ahead, at most
  :data:`MCP_NOTICE_MAX_CREATED_PER_DAY` created in any 24 hours (withdrawn
  ones count). Both counts run under one transaction-level advisory lock.
- Delivery: ``bootstrap`` reads the candidates (:func:`read_notice_candidates`)
  and, after the reply is otherwise built, claims the ones that fit with
  :func:`claim_notice_deliveries`: one ``INSERT ... SELECT ... ON CONFLICT DO
  NOTHING RETURNING`` that re-checks activeness, the person's newest answer,
  that no erasure request is pending, cooling off or in progress, and first
  delivery, together. Only the returned ids are placed, so a notice reaches a
  person at most once across every client. All times are the database's
  ``now()``.

Nothing here is reachable from the anonymous public MCP endpoint, share keys,
agent-bound or connector keys, or any tool but ``bootstrap``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from sqlalchemy import String, case, exists, func, literal, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.auth import AuditLog, User
from models.erasure import STATUS_COOLING_OFF, STATUS_IN_PROGRESS, STATUS_PENDING, ErasureRequest
from models.mcp_notice import McpNotice, McpNoticeConsent, McpNoticeDelivery
from services.mcp_notice_text import NoticeTextError, normalize_notice_text
from utils.exceptions import (
    McpNoticeActiveLimitError,
    McpNoticeDailyLimitError,
    McpNoticeNotFoundError,
    McpNoticeTextRejectedError,
    McpNoticeWindowInvalidError,
    NotFoundException,
)
from utils.hashing import sha256_hex

__all__ = [
    "MCP_NOTICES_WORDING_VERSION",
    "MCP_NOTICE_BUDGET_SHARE",
    "MCP_NOTICE_MAX_ACTIVE",
    "MCP_NOTICE_MAX_CREATED_PER_DAY",
    "MCP_NOTICE_MAX_DAYS",
    "MCP_NOTICE_MAX_SCHEDULE_DAYS",
    "McpNoticeConsentRecordResult",
    "McpNoticeConsentState",
    "NoticeActor",
    "NoticeCandidate",
    "NoticeListState",
    "NoticeView",
    "claim_notice_deliveries",
    "create_notice",
    "current_mcp_notice_consent",
    "current_mcp_notice_wording_version",
    "list_notices",
    "mcp_notices_enabled",
    "read_notice_candidates",
    "record_mcp_notice_consent",
    "withdraw_notice",
]

# The version of the consent copy (``profile.mcpNotices.*`` in the message
# catalogs). Bump it in the same change as the copy; a "yes" records the
# version the person saw. A bump does not withdraw earlier answers.
MCP_NOTICES_WORDING_VERSION: Final = "2026-11"

# Documented caps (docs/deployment.md, "Service notices over MCP").
MCP_NOTICE_MAX_ACTIVE: Final = 3
MCP_NOTICE_MAX_DAYS: Final = 30
MCP_NOTICE_MAX_SCHEDULE_DAYS: Final = 90
MCP_NOTICE_MAX_CREATED_PER_DAY: Final = 5
# ``starts_at`` may lie this far in the past (clock skew), no further.
MCP_NOTICE_BACKDATE_SECONDS: Final = 60
# The notices block takes at most ``max_chars // MCP_NOTICE_BUDGET_SHARE`` of a
# bootstrap reply (the plan_headroom rule).
MCP_NOTICE_BUDGET_SHARE: Final = 4
# More rows than this are never read for one reply (the cap keeps it at 3).
_CANDIDATE_LIMIT: Final = 10

# Serialises creates so the two counts cannot both pass for two writers.
_CREATE_LOCK_SQL: Final = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('mcp_notices_create', 0))"
)
_CLAIM_LOCK_TIMEOUT_SQL: Final = text("SET LOCAL lock_timeout = '1s'")
_CLAIM_STATEMENT_TIMEOUT_SQL: Final = text("SET LOCAL statement_timeout = '2s'")

_ACTIVE_ERASURE_STATUSES: Final = (STATUS_PENDING, STATUS_COOLING_OFF, STATUS_IN_PROGRESS)
_CONSENT_SOURCE: Final = "settings"

NoticeState = Literal["scheduled", "active", "ended"]
NoticeListState = Literal["active", "scheduled", "ended", "all"]


# ---------------------------------------------------------------------------
# Offer and consent
# ---------------------------------------------------------------------------


def mcp_notices_enabled() -> bool:
    """Whether the deployment offers service notices over MCP."""
    return bool(get_settings().mcp_notices_enabled)


def current_mcp_notice_wording_version() -> str | None:
    """The consent copy version to show and send back, or ``None`` while off."""
    if not mcp_notices_enabled():
        return None
    return MCP_NOTICES_WORDING_VERSION


@dataclass(frozen=True)
class McpNoticeConsentState:
    """The newest consent row of one user."""

    value: bool
    source: str
    wording_version: str | None
    recorded_at: datetime


@dataclass(frozen=True)
class McpNoticeConsentRecordResult:
    """What :func:`record_mcp_notice_consent` did (``recorded`` false: unchanged)."""

    state: McpNoticeConsentState
    recorded: bool


async def current_mcp_notice_consent(
    session: AsyncSession, user_id: str
) -> McpNoticeConsentState | None:
    """The user's newest answer, or ``None`` when they never answered."""
    row = (
        await session.execute(
            select(
                McpNoticeConsent.value,
                McpNoticeConsent.source,
                McpNoticeConsent.wording_version,
                McpNoticeConsent.recorded_at,
            )
            .where(McpNoticeConsent.user_id == user_id)
            .order_by(McpNoticeConsent.id.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return McpNoticeConsentState(
        value=row.value,
        source=row.source,
        wording_version=row.wording_version,
        recorded_at=row.recorded_at,
    )


async def record_mcp_notice_consent(
    session: AsyncSession,
    *,
    user_id: str,
    value: bool,
    wording_version: str | None,
) -> McpNoticeConsentRecordResult:
    """Record an answer (source ``settings``). Flushes; the caller commits.

    Serialised per user by a row lock on the ``users`` row, so two concurrent
    changes cannot both read the same newest row. Appends only when the answer
    changes ("the same": the same value and, for a "yes", the same wording
    version). A withdrawal stores no wording version.

    Raises:
        ValueError: ``value`` is true without a ``wording_version``.
        NotFoundException: No ``users`` row (the account is gone).
    """
    if value and not wording_version:
        raise ValueError("an opt-in must name the wording version the person saw")
    locked = await session.scalar(
        select(User.user_id).where(User.user_id == user_id).with_for_update()
    )
    if locked is None:
        raise NotFoundException("User", user_id)
    current = await current_mcp_notice_consent(session, user_id)
    if current is not None and current.value == value:
        if not value or current.wording_version == wording_version:
            return McpNoticeConsentRecordResult(state=current, recorded=False)
    row = McpNoticeConsent(
        user_id=user_id,
        value=value,
        source=_CONSENT_SOURCE,
        wording_version=wording_version if value else None,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row, attribute_names=["recorded_at"])
    return McpNoticeConsentRecordResult(
        state=McpNoticeConsentState(
            value=row.value,
            source=row.source,
            wording_version=row.wording_version,
            recorded_at=row.recorded_at,
        ),
        recorded=True,
    )


# ---------------------------------------------------------------------------
# Writing notices (admin)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NoticeActor:
    """Who writes: the admin's email / user_id, or ``operator:<address>`` twice."""

    email: str
    user_id: str
    ip_address: str | None = None
    user_agent: str | None = None


@dataclass(frozen=True)
class NoticeView:
    """A notice as the admin routes show it (``delivered_count`` is an aggregate)."""

    id: uuid.UUID
    text: str
    starts_at: datetime
    expires_at: datetime
    withdrawn_at: datetime | None
    created_at: datetime
    state: NoticeState
    delivered_count: int


def _state_at(
    now: datetime, starts_at: datetime, expires_at: datetime, withdrawn_at: datetime | None
) -> NoticeState:
    if withdrawn_at is not None or expires_at <= now:
        return "ended"
    if starts_at > now:
        return "scheduled"
    return "active"


def _check_window(now: datetime, starts_at: datetime, expires_at: datetime) -> None:
    if expires_at <= starts_at:
        raise McpNoticeWindowInvalidError(
            "ends_before_start", "expires_at must be later than starts_at."
        )
    if expires_at <= now:
        raise McpNoticeWindowInvalidError("already_expired", "expires_at is in the past.")
    if starts_at < now - timedelta(seconds=MCP_NOTICE_BACKDATE_SECONDS):
        raise McpNoticeWindowInvalidError(
            "starts_in_the_past", "starts_at may not lie in the past; omit it to start now."
        )
    if starts_at > now + timedelta(days=MCP_NOTICE_MAX_SCHEDULE_DAYS):
        raise McpNoticeWindowInvalidError(
            "starts_too_far_ahead",
            f"starts_at may be at most {MCP_NOTICE_MAX_SCHEDULE_DAYS} days ahead.",
        )
    if expires_at - starts_at > timedelta(days=MCP_NOTICE_MAX_DAYS):
        raise McpNoticeWindowInvalidError(
            "too_long", f"A notice may run for at most {MCP_NOTICE_MAX_DAYS} days."
        )


def _audit(
    actor: NoticeActor, action: str, notice_id: uuid.UUID, notice_text: str, **meta: Any
) -> AuditLog:
    return AuditLog(
        user_email=actor.email,
        user_id=actor.user_id,
        action=action,
        resource=f"mcp_notice:{notice_id}",
        new_value_hash=sha256_hex(notice_text),
        user_metadata={"text_chars": len(notice_text), **meta},
        ip_address=actor.ip_address[:45] if actor.ip_address else None,
        user_agent=actor.user_agent[:512] if actor.user_agent else None,
    )


async def create_notice(
    session: AsyncSession,
    *,
    raw_text: str,
    expires_at: datetime,
    starts_at: datetime | None,
    actor: NoticeActor,
) -> NoticeView:
    """Write a notice and its audit row. Flushes; the caller commits.

    ``starts_at`` absent = now. The text is normalised first (nothing is
    locked for a text that is refused), then the create lock is taken and the
    window, the active count and the 24-hour count are checked against the
    database clock.

    Raises:
        McpNoticeTextRejectedError: 422 ``MCPNOTICE-001``.
        McpNoticeWindowInvalidError: 422 ``MCPNOTICE-002``.
        McpNoticeActiveLimitError: 409 ``MCPNOTICE-003``.
        McpNoticeDailyLimitError: 409 ``MCPNOTICE-007``.
    """
    try:
        notice_text = normalize_notice_text(raw_text)
    except NoticeTextError as exc:
        raise McpNoticeTextRejectedError(exc.reason) from None

    await session.execute(_CREATE_LOCK_SQL)
    now: datetime = (await session.execute(select(func.now()))).scalar_one()
    start = starts_at if starts_at is not None else now
    _check_window(now, start, expires_at)

    active = (
        await session.execute(
            select(func.count())
            .select_from(McpNotice)
            .where(McpNotice.withdrawn_at.is_(None), McpNotice.expires_at > now)
        )
    ).scalar_one()
    if active >= MCP_NOTICE_MAX_ACTIVE:
        raise McpNoticeActiveLimitError(MCP_NOTICE_MAX_ACTIVE)
    created = (
        await session.execute(
            select(func.count())
            .select_from(McpNotice)
            .where(McpNotice.created_at > now - timedelta(hours=24))
        )
    ).scalar_one()
    if created >= MCP_NOTICE_MAX_CREATED_PER_DAY:
        raise McpNoticeDailyLimitError(MCP_NOTICE_MAX_CREATED_PER_DAY)

    notice = McpNotice(
        id=uuid.uuid4(),
        text=notice_text,
        starts_at=start,
        expires_at=expires_at,
        created_at=now,
    )
    session.add(notice)
    session.add(
        _audit(
            actor,
            "mcp_notice.created",
            notice.id,
            notice_text,
            starts_at=start.isoformat(),
            expires_at=expires_at.isoformat(),
        )
    )
    await session.flush()
    return NoticeView(
        id=notice.id,
        text=notice_text,
        starts_at=start,
        expires_at=expires_at,
        withdrawn_at=None,
        created_at=now,
        state=_state_at(now, start, expires_at, None),
        delivered_count=0,
    )


def _delivered_count() -> Any:
    return (
        select(func.count())
        .select_from(McpNoticeDelivery)
        .where(McpNoticeDelivery.notice_id == McpNotice.id)
        .correlate(McpNotice)
        .scalar_subquery()
    )


def _state_column() -> Any:
    now = func.now()
    return case(
        (McpNotice.withdrawn_at.is_not(None), literal("ended")),
        (McpNotice.expires_at <= now, literal("ended")),
        (McpNotice.starts_at > now, literal("scheduled")),
        else_=literal("active"),
    )


def _view_query() -> Any:
    return select(
        McpNotice.id,
        McpNotice.text,
        McpNotice.starts_at,
        McpNotice.expires_at,
        McpNotice.withdrawn_at,
        McpNotice.created_at,
        _state_column().label("state"),
        _delivered_count().label("delivered_count"),
    )


def _view(row: Any) -> NoticeView:
    return NoticeView(
        id=row.id,
        text=row.text,
        starts_at=row.starts_at,
        expires_at=row.expires_at,
        withdrawn_at=row.withdrawn_at,
        created_at=row.created_at,
        state=row.state,
        delivered_count=int(row.delivered_count),
    )


async def list_notices(
    session: AsyncSession, *, state: NoticeListState = "all", limit: int = 50
) -> list[NoticeView]:
    """Notices, newest first, with an aggregate ``delivered_count`` each."""
    query = _view_query().order_by(McpNotice.created_at.desc(), McpNotice.id.desc()).limit(limit)
    if state != "all":
        query = query.where(_state_column() == state)
    return [_view(row) for row in (await session.execute(query)).all()]


async def withdraw_notice(
    session: AsyncSession, *, notice_id: uuid.UUID, actor: NoticeActor
) -> tuple[NoticeView, bool]:
    """End a notice now. Idempotent. Flushes; the caller commits.

    Returns the notice and whether this call withdrew it. An ended notice
    (withdrawn, or expired on its own) is returned unchanged and writes no
    audit row, so an expired notice keeps saying it expired. Whether it has
    ended is decided once, on the locked row against the transaction's
    ``now()``. Delivery stops at once: the claim re-checks ``withdrawn_at``.

    Raises:
        McpNoticeNotFoundError: 404 ``MCPNOTICE-004``.
    """
    row = (
        await session.execute(
            select(
                McpNotice.id,
                McpNotice.text,
                (McpNotice.withdrawn_at.is_(None) & (McpNotice.expires_at > func.now())).label(
                    "live_or_scheduled"
                ),
            )
            .where(McpNotice.id == notice_id)
            .with_for_update()
        )
    ).first()
    if row is None:
        raise McpNoticeNotFoundError()
    changed = bool(row.live_or_scheduled)
    if changed:
        await session.execute(
            update(McpNotice).where(McpNotice.id == notice_id).values(withdrawn_at=func.now())
        )
        session.add(_audit(actor, "mcp_notice.withdrawn", notice_id, row.text))
        await session.flush()
    view = (await session.execute(_view_query().where(McpNotice.id == notice_id))).one()
    return _view(view), changed


# ---------------------------------------------------------------------------
# Delivery (bootstrap)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NoticeCandidate:
    """A notice that may be placed in this person's reply."""

    id: uuid.UUID
    text: str
    starts_at: datetime
    expires_at: datetime
    created_at: datetime


def _deliverable_to(user_id: str) -> list[Any]:
    """Live now, the person's newest answer is "yes", no erasure under way."""
    now = func.now()
    newest_answer = (
        select(McpNoticeConsent.value)
        .where(McpNoticeConsent.user_id == user_id)
        .order_by(McpNoticeConsent.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    erasing = exists().where(
        ErasureRequest.user_id == user_id,
        ErasureRequest.status.in_(_ACTIVE_ERASURE_STATUSES),
    )
    return [
        McpNotice.withdrawn_at.is_(None),
        McpNotice.starts_at <= now,
        McpNotice.expires_at > now,
        newest_answer.is_(True),
        ~erasing,
    ]


async def read_notice_candidates(session: AsyncSession, user_id: str) -> list[NoticeCandidate]:
    """Notices to offer this person now, in delivery order. Read only.

    Order: ``expires_at``, then ``starts_at``, then ``id``. Nothing when the
    person never turned notices on, turned them off, has an erasure request
    under way, or already got every live notice.
    """
    delivered = exists().where(
        McpNoticeDelivery.user_id == user_id,
        McpNoticeDelivery.notice_id == McpNotice.id,
    )
    rows = (
        await session.execute(
            select(
                McpNotice.id,
                McpNotice.text,
                McpNotice.starts_at,
                McpNotice.expires_at,
                McpNotice.created_at,
            )
            .where(*_deliverable_to(user_id), ~delivered)
            .order_by(McpNotice.expires_at, McpNotice.starts_at, McpNotice.id)
            .limit(_CANDIDATE_LIMIT)
        )
    ).all()
    return [
        NoticeCandidate(
            id=row.id,
            text=row.text,
            starts_at=row.starts_at,
            expires_at=row.expires_at,
            created_at=row.created_at,
        )
        for row in rows
    ]


async def claim_notice_deliveries(
    session: AsyncSession, *, user_id: str, notice_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """Claim the fitted notices for this person and commit; returns the ids won.

    ONE statement decides activeness, the newest answer, the erasure state and
    first delivery together (``INSERT ... SELECT ... ON CONFLICT DO NOTHING
    RETURNING``), so a withdraw, an opt-out or a concurrent session start
    between the read and this call removes the notice instead of delivering it
    twice or against the person's answer. The database bounds the wait
    (``lock_timeout`` 1 s, ``statement_timeout`` 2 s, set first in the
    transaction); no client-side timeout wraps the insert or the commit.

    ``session`` must be a fresh session of its own: the caller opens it only
    when there is something to claim, catches every exception, closes it and
    never retries.
    """
    if not notice_ids:
        return set()
    await session.execute(_CLAIM_LOCK_TIMEOUT_SQL)
    await session.execute(_CLAIM_STATEMENT_TIMEOUT_SQL)
    chosen = select(literal(user_id, type_=String(255)), McpNotice.id).where(
        McpNotice.id.in_(notice_ids), *_deliverable_to(user_id)
    )
    statement = (
        pg_insert(McpNoticeDelivery)
        .from_select(["user_id", "notice_id"], chosen)
        .on_conflict_do_nothing()
        .returning(McpNoticeDelivery.notice_id)
    )
    won = set((await session.execute(statement)).scalars().all())
    await session.commit()
    return won
