"""Announcement-email consent history (Issue #2761).

``announcement_email_consents`` is append-only: one row per recorded answer,
never updated. A user's current answer is the value on their newest row (the
highest ``id``); a user with no row has never answered and gets no
announcement email. There is no copy of the answer on ``users``, so the history
and the current answer cannot disagree.

Only announcement mail (new features, product news) consults this table.
Service notices (invitations, password, security, capacity, erasure) never do.
See ``services/announcement_consent_service.py`` for when a row is written.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

# Where an answer came from. Byte-identical to the CHECK below and to the f38
# migration (schema drift test).
AnnouncementConsentSource = Literal["signup", "settings", "unsubscribe_link"]
ANNOUNCEMENT_CONSENT_SOURCES: tuple[AnnouncementConsentSource, ...] = (
    "signup",
    "settings",
    "unsubscribe_link",
)


class AnnouncementEmailConsent(Base):
    """One recorded answer to "send me announcement emails".

    Attributes:
        id: Identity primary key. Insertion order breaks ties between rows
            written in one transaction (they share ``recorded_at``), so the
            newest row is the one with the highest ``id``.
        user_id: The user's ID (OAuth sub). ``ON DELETE CASCADE`` — the
            history goes with the account.
        value: True when the user wants announcement emails.
        wording_version: The version of the consent copy the person saw.
            Required when ``value`` is true; NULL for a withdrawal.
        source: ``signup`` (the sign-up checkbox), ``settings`` (the toggle in
            account settings) or ``unsubscribe_link`` (the link in an
            announcement email).
        recorded_at: When the row was written (timezone-aware).
    """

    __tablename__ = "announcement_email_consents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    value: Mapped[bool] = mapped_column(Boolean, nullable=False)
    wording_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "source IN ('signup', 'settings', 'unsubscribe_link')",
            name="valid_announcement_consent_source",
        ),
        # A "yes" must name the copy the person agreed to.
        CheckConstraint(
            "value = false OR wording_version IS NOT NULL",
            name="ck_announcement_consent_version_when_on",
        ),
        # Serves "newest row for this user" and the per-user DISTINCT ON scan.
        Index("ix_announcement_email_consents_user_id_id", "user_id", "id"),
    )

    def __repr__(self) -> str:
        return (
            f"<AnnouncementEmailConsent(user_id='{self.user_id}', value={self.value}, "
            f"source='{self.source}')>"
        )
