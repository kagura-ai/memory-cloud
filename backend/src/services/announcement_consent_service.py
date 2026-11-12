"""Announcement-email consent (Issue #2761).

Whether a person wants announcement emails (new features, product news) is an
explicit, recorded answer:

- ``ANNOUNCEMENT_EMAILS_ENABLED`` (default off) decides whether the deployment
  offers them. While it is off nobody is asked, nobody can opt in and
  :func:`select_announcement_recipients` selects nobody; turning the setting
  OFF in account settings and the unsubscribe link keep working.
- Every answer is a row in ``announcement_email_consents`` (append-only; the
  newest row is the current answer). Sources: ``signup`` (the unchecked-by-
  default sign-up checkbox, new accounts only), ``settings`` (the toggle in
  account settings) and ``unsubscribe_link`` (the link in an announcement
  email, see ``auth/announcement_unsubscribe.py``).
- The wording version is a code constant, bumped in the same change as the
  consent copy in the message catalogs.

ANY CODE THAT SENDS ANNOUNCEMENT EMAIL must pick its recipients with
:func:`select_announcement_recipients`, right before each batch, and put the
recipient's ``list_unsubscribe_headers`` on the message and its
``unsubscribe_page_url`` in the body. Service notices (invitations, password,
security, capacity, erasure) never consult this module and keep being sent.

Sign-up paths (the OAuth callbacks today, the email sign-up form later) write
the sign-up answer only in the transaction that creates the ``users`` row,
after their admission decision, with :func:`resolve_signup_consent` and
:func:`new_consent_row`. A sign-up that meets an existing account writes
nothing. An email sign-up keeps the answer server-side until the address is
verified (it never travels in the verification link); the recipient helper's
``email_verified_at`` filter is what keeps mail away from an unverified
address.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.announcement_unsubscribe import (
    UnsubscribeClaim,
    build_unsubscribe_headers,
    unsubscribe_page_url,
)
from config.settings import get_settings
from models.announcement_consent import (
    ANNOUNCEMENT_CONSENT_SOURCES,
    AnnouncementConsentSource,
    AnnouncementEmailConsent,
)
from models.auth import User
from models.erasure import STATUS_COOLING_OFF, STATUS_IN_PROGRESS, STATUS_PENDING, ErasureRequest
from utils.exceptions import NotFoundException
from utils.logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "ANNOUNCEMENT_CONSENT_WORDING_VERSION",
    "AnnouncementConsentSource",
    "AnnouncementRecipient",
    "ConsentRecordResult",
    "ConsentState",
    "SignupAnnouncementConsent",
    "announcement_emails_enabled",
    "apply_unsubscribe",
    "current_announcement_consent",
    "current_consent_wording_version",
    "new_consent_row",
    "record_announcement_consent",
    "resolve_signup_consent",
    "select_announcement_recipients",
]

# The version of the consent copy (``announcementEmails.optIn`` and
# ``profile.announcementEmails.*`` in the message catalogs). Bump it in the
# same change as the copy; a "yes" records the version the person saw.
ANNOUNCEMENT_CONSENT_WORDING_VERSION: Final = "2026-11"

# Erasure requests in these states exclude a user from announcements.
_ACTIVE_ERASURE_STATUSES: Final = (STATUS_PENDING, STATUS_COOLING_OFF, STATUS_IN_PROGRESS)


def announcement_emails_enabled() -> bool:
    """Whether the deployment offers announcement emails."""
    return bool(get_settings().announcement_emails_enabled)


def current_consent_wording_version() -> str | None:
    """The consent copy version to show and send back, or ``None`` while off."""
    if not announcement_emails_enabled():
        return None
    return ANNOUNCEMENT_CONSENT_WORDING_VERSION


@dataclass(frozen=True)
class SignupAnnouncementConsent:
    """The answer a sign-up carried: the box was shown with this copy."""

    value: bool
    wording_version: str


def resolve_signup_consent(
    value: object, wording_version: object
) -> SignupAnnouncementConsent | None:
    """The sign-up answer to record, or ``None`` to record nothing.

    ``None`` unless the feature is on, ``value`` is a real bool and
    ``wording_version`` is the current version (a page loaded before a copy
    change records nothing rather than a version the person did not see).
    Never raises: an absent or invalid value never blocks a sign-up.
    """
    current = current_consent_wording_version()
    if current is None:
        return None
    if type(value) is not bool:
        return None
    if not isinstance(wording_version, str) or wording_version != current:
        return None
    return SignupAnnouncementConsent(value=value, wording_version=current)


def _check_row(value: bool, source: str, wording_version: str | None) -> None:
    if source not in ANNOUNCEMENT_CONSENT_SOURCES:
        raise ValueError(f"unknown announcement consent source: {source!r}")
    if value and not wording_version:
        raise ValueError("an opt-in must name the wording version the person saw")


def new_consent_row(
    *,
    user_id: str,
    value: bool,
    source: AnnouncementConsentSource,
    wording_version: str | None,
) -> AnnouncementEmailConsent:
    """An unsaved history row (pure builder, no I/O).

    For a transaction that is creating the user: the caller ``add``s it next to
    the new ``users`` row and commits both together.

    Raises:
        ValueError: ``value`` is true without a ``wording_version``, or
            ``source`` is not one of the three sources.
    """
    _check_row(value, source, wording_version)
    return AnnouncementEmailConsent(
        user_id=user_id,
        value=value,
        source=source,
        wording_version=wording_version,
    )


@dataclass(frozen=True)
class ConsentState:
    """The newest history row of one user."""

    value: bool
    source: str
    wording_version: str | None
    recorded_at: datetime


async def current_announcement_consent(session: AsyncSession, user_id: str) -> ConsentState | None:
    """The user's newest answer, or ``None`` when they never answered."""
    row = (
        await session.execute(
            select(
                AnnouncementEmailConsent.value,
                AnnouncementEmailConsent.source,
                AnnouncementEmailConsent.wording_version,
                AnnouncementEmailConsent.recorded_at,
            )
            .where(AnnouncementEmailConsent.user_id == user_id)
            .order_by(AnnouncementEmailConsent.id.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return ConsentState(
        value=row.value,
        source=row.source,
        wording_version=row.wording_version,
        recorded_at=row.recorded_at,
    )


@dataclass(frozen=True)
class ConsentRecordResult:
    """What :func:`record_announcement_consent` did.

    Attributes:
        state: The user's newest answer after the call.
        recorded: False when the answer was already the newest one, so no row
            was written.
    """

    state: ConsentState
    recorded: bool


async def _append_if_changed(
    session: AsyncSession,
    *,
    user_id: str,
    value: bool,
    source: AnnouncementConsentSource,
    wording_version: str | None,
) -> ConsentRecordResult:
    """Append a row unless the newest one already says the same.

    "The same": the same value and, for a "yes", the same wording version (a
    "yes" to newer copy is recorded again). The caller holds the users-row
    lock. Flushes; never commits.
    """
    current = await current_announcement_consent(session, user_id)
    unchanged = current is not None and current.value == value
    if unchanged and value and current is not None and current.wording_version != wording_version:
        unchanged = False
    if unchanged and current is not None:
        return ConsentRecordResult(state=current, recorded=False)

    row = new_consent_row(
        user_id=user_id,
        value=value,
        source=source,
        wording_version=wording_version if value else None,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row, attribute_names=["recorded_at"])
    return ConsentRecordResult(
        state=ConsentState(
            value=row.value,
            source=row.source,
            wording_version=row.wording_version,
            recorded_at=row.recorded_at,
        ),
        recorded=True,
    )


async def record_announcement_consent(
    session: AsyncSession,
    *,
    user_id: str,
    value: bool,
    source: AnnouncementConsentSource,
    wording_version: str | None,
) -> ConsentRecordResult:
    """Record an answer for an existing user. Flushes; the caller commits.

    Serialized per user by a row lock on the ``users`` row (``SELECT ... FOR
    UPDATE``, as ``TermsService``), so two concurrent changes cannot both read
    the same newest row and append in the wrong order. A withdrawal stores no
    wording version. Idempotent: an unchanged answer writes nothing.

    Not for a transaction that is creating the user: use
    :func:`new_consent_row` there (this function's reads would autoflush the
    pending ``users`` insert).

    Raises:
        ValueError: ``value`` is true without a ``wording_version``.
        NotFoundException: No ``users`` row (the account is gone).
    """
    _check_row(value, source, wording_version)
    locked = await session.scalar(
        select(User.user_id).where(User.user_id == user_id).with_for_update()
    )
    if locked is None:
        raise NotFoundException("User", user_id)
    return await _append_if_changed(
        session, user_id=user_id, value=value, source=source, wording_version=wording_version
    )


async def apply_unsubscribe(session: AsyncSession, claim: UnsubscribeClaim) -> bool:
    """Turn announcement emails off for the account a valid token names.

    Does nothing (returns False) when no ``users`` row matches BOTH the row id
    and the ``user_id`` of the token — an erased account, or a later account
    that reuses the subject — or when the setting is already off. Can change
    nothing else. Flushes; the caller commits.
    """
    locked = await session.scalar(
        select(User.user_id)
        .where(User.id == claim.account_id, User.user_id == claim.user_id)
        .with_for_update()
    )
    if locked is None:
        return False
    result = await _append_if_changed(
        session,
        user_id=claim.user_id,
        value=False,
        source="unsubscribe_link",
        wording_version=None,
    )
    return result.recorded


@dataclass(frozen=True)
class AnnouncementRecipient:
    """One person who asked for announcement emails, with the opt-out material.

    Attributes:
        user_id: The account's user ID.
        email: The verified address to send to.
        locale: The account's UI language (``en`` / ``ja``).
        list_unsubscribe_headers: ``List-Unsubscribe`` + ``List-Unsubscribe-
            Post`` for this recipient; put them on the message as they are.
        unsubscribe_page_url: The unsubscribe link for the message body.
    """

    user_id: str
    email: str
    locale: str
    list_unsubscribe_headers: dict[str, str] = field(compare=False, repr=False)
    unsubscribe_page_url: str = field(compare=False, repr=False)


async def select_announcement_recipients(
    session: AsyncSession, *, after_user_id: str | None = None, limit: int = 500
) -> list[AnnouncementRecipient]:
    """THE way to pick announcement recipients: who asked for them, now.

    Returns only users whose newest consent row is "on", whose address is
    verified (``email_verified_at`` set) and not a local CLI address
    (``@local``), and who have no erasure request pending, cooling off or in
    progress. Nobody while ``ANNOUNCEMENT_EMAILS_ENABLED`` is off. Ordered by
    ``user_id`` and paged by keyset: pass the last ``user_id`` of a page as
    ``after_user_id`` to get the next one.

    Call it right before each batch is sent: a person who withdraws between
    selection and sending must not get the email. Service notices never use
    this helper.
    """
    if not announcement_emails_enabled() or limit <= 0:
        return []

    newest = (
        select(AnnouncementEmailConsent.user_id, AnnouncementEmailConsent.value)
        .distinct(AnnouncementEmailConsent.user_id)
        .order_by(AnnouncementEmailConsent.user_id, AnnouncementEmailConsent.id.desc())
        .subquery("newest_consent")
    )
    active_erasure = (
        select(ErasureRequest.id)
        .where(
            ErasureRequest.user_id == User.user_id,
            ErasureRequest.status.in_(_ACTIVE_ERASURE_STATUSES),
        )
        .exists()
    )
    stmt = (
        select(User.id, User.user_id, User.email, User.locale)
        .join(newest, newest.c.user_id == User.user_id)
        .where(
            newest.c.value.is_(True),
            User.email_verified_at.is_not(None),
            ~func.lower(func.trim(User.email)).like("%@local"),
            ~active_erasure,
        )
        .order_by(User.user_id)
        .limit(limit)
    )
    if after_user_id is not None:
        stmt = stmt.where(User.user_id > after_user_id)

    rows = (await session.execute(stmt)).all()
    return [
        AnnouncementRecipient(
            user_id=row.user_id,
            email=row.email,
            locale=row.locale,
            list_unsubscribe_headers=build_unsubscribe_headers(
                account_id=row.id, user_id=row.user_id
            ),
            unsubscribe_page_url=unsubscribe_page_url(account_id=row.id, user_id=row.user_id),
        )
        for row in rows
    ]
