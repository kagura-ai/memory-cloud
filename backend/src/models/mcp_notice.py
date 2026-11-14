"""Service notices over MCP (Issue #2762): notices, deliveries, consent history.

Three tables:

- ``mcp_notices``: a short plain-text notice an administrator of this server
  wrote, live from ``starts_at`` until ``expires_at`` (or ``withdrawn_at``).
  The text is immutable (a correction is withdraw + create) and stored in the
  normalised form ``services.mcp_notice_text`` produces. ``plans`` (#2800)
  names the plan tiers it is for, in tier order; ``NULL`` is every plan. No
  actor column: who wrote it is in ``audit_logs``, so the row holds no
  personal identifier.
- ``mcp_notice_deliveries``: one row per (user, notice), written when the
  notice was placed in a ``bootstrap`` reply for that person. The primary key
  makes "at most once per user" hold across every client.
- ``mcp_notice_consents``: append-only history of the per-user opt-in, the
  shape of ``announcement_email_consents`` (#2761) in a table of its own, so an
  answer for one purpose can never be read as an answer for the other. The
  newest row (highest ``id``) is the current answer; no row = never answered =
  off.

Consent and delivery rows go with the user through ``ON DELETE CASCADE`` in
the database; no ORM relationship to ``User`` is declared, so the cascade
stays there. See ``services/mcp_notice_service.py``.
"""

from __future__ import annotations

import uuid
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
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

# Where an answer came from. Byte-identical to the CHECK below and to the f42
# migration (schema drift test).
McpNoticeConsentSource = Literal["settings"]
MCP_NOTICE_CONSENT_SOURCES: tuple[McpNoticeConsentSource, ...] = ("settings",)


class McpNotice(Base):
    """One service notice.

    Attributes:
        id: Server-generated UUIDv4.
        text: The normalised text, 1-300 characters, at most 900 UTF-8 bytes.
        starts_at: When it becomes deliverable.
        expires_at: When it stops being deliverable (after ``starts_at``).
        withdrawn_at: Set when an administrator ended it early.
        created_at: When it was written.
        plans: The plan tiers it is for (#2800): tier keys in tier order, at
            least one (CHECK); ``NULL`` = every plan. Delivered only in a
            ``bootstrap`` whose context's workspace is on one of them. The
            names are checked by the API against the tier registry, not by
            the database, so a new tier needs no change here.
    """

    __tablename__ = "mcp_notices"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    text: Mapped[str] = mapped_column(String(300), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    plans: Mapped[list[str] | None] = mapped_column(ARRAY(String(50)), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "char_length(text) BETWEEN 1 AND 300 AND octet_length(text) <= 900",
            name="ck_mcp_notices_text_size",
        ),
        CheckConstraint("expires_at > starts_at", name="ck_mcp_notices_window"),
        # #2800: byte-identical to the f45 migration (schema drift test).
        CheckConstraint(
            "plans IS NULL OR cardinality(plans) >= 1", name="ck_mcp_notices_plans_nonempty"
        ),
        # Serves the delivery read and the active count.
        Index(
            "ix_mcp_notices_active",
            "expires_at",
            postgresql_where=sql_text("withdrawn_at IS NULL"),
        ),
    )

    def __repr__(self) -> str:
        return f"<McpNotice(id='{self.id}', expires_at={self.expires_at})>"


class McpNoticeDelivery(Base):
    """A notice placed in one person's ``bootstrap`` reply (once per user).

    Attributes:
        user_id: The person (OAuth sub). ``ON DELETE CASCADE``.
        notice_id: The notice. ``ON DELETE CASCADE``.
        delivered_at: When it was claimed.
    """

    __tablename__ = "mcp_notice_deliveries"

    user_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    notice_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("mcp_notices.id", ondelete="CASCADE"),
        primary_key=True,
    )
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # The primary key (user_id, notice_id) serves "not yet delivered to
        # this user"; this index serves the cascade from mcp_notices and the
        # per-notice delivered count.
        Index("ix_mcp_notice_deliveries_notice_id", "notice_id"),
    )

    def __repr__(self) -> str:
        return f"<McpNoticeDelivery(user_id='{self.user_id}', notice_id='{self.notice_id}')>"


class McpNoticeConsent(Base):
    """One recorded answer to "show me service notices in my AI tools".

    Attributes:
        id: Identity primary key; the newest row (highest ``id``) is the answer.
        user_id: The user's ID (OAuth sub). ``ON DELETE CASCADE``.
        value: True when the user wants notices.
        wording_version: The consent copy version the person saw. Required
            when ``value`` is true; NULL for a withdrawal.
        source: ``settings`` (the toggle in account settings).
        recorded_at: When the row was written (timezone-aware).
    """

    __tablename__ = "mcp_notice_consents"

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
        CheckConstraint("source IN ('settings')", name="valid_mcp_notice_consent_source"),
        # A "yes" must name the copy the person agreed to.
        CheckConstraint(
            "value = false OR wording_version IS NOT NULL",
            name="ck_mcp_notice_consent_version_when_on",
        ),
        # Serves "newest row for this user".
        Index("ix_mcp_notice_consents_user_id_id", "user_id", "id"),
    )

    def __repr__(self) -> str:
        return (
            f"<McpNoticeConsent(user_id='{self.user_id}', value={self.value}, "
            f"source='{self.source}')>"
        )
