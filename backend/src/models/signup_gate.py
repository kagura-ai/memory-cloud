"""Signup gate SQLAlchemy models (Issue #358, extended in #655).

Phase 1 of admin-configurable signup gate. The single-row
``signup_gate_config`` holds the runtime switch + mode; ``signup_allowlist``
keys each allowed registrant on a stable, provider-specific immutable
identifier (GitHub numeric ID, Google OIDC ``sub`` claim — neither changes
when the user renames/changes email at the IdP).

Sponsors-related columns on ``SignupGateConfig`` are reserved in Phase 1 and
populated by Phase 2's GraphQL sync + webhook flow.

#655 extended the allowlist to support Google entries via a ``provider`` /
``subject_id`` / ``subject_label`` triplet that replaces the GitHub-only
``github_user_id``/``github_username`` matching keys. The old columns are
kept NOT NULL during the migration window (deprecated comment) — physical
drop is deferred to a future issue.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from db.base import Base


class SignupGateConfig(Base):
    """Single-row config table for the signup gate.

    Always exactly one row with id=1. The CheckConstraint + migration seed
    enforce the singleton; ``SignupGateService._load_config`` also self-heals
    if the row is missing.
    """

    __tablename__ = "signup_gate_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    mode: Mapped[str] = mapped_column(String(20), nullable=False, server_default="manual")

    # --- Phase 2 (reserved — unused in Phase 1) ---
    github_sponsors_account: Mapped[str | None] = mapped_column(String(255), nullable=True)
    github_sponsors_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_sponsors_webhook_secret_encrypted: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    github_sponsors_min_tier_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    github_sponsors_grace_period_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="30"
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "mode IN ('manual', 'github_sponsors', 'both')",
            name="valid_signup_gate_mode",
        ),
        CheckConstraint("id = 1", name="signup_gate_config_singleton"),
    )


class SignupAllowlistEntry(Base):
    """A single entry on the signup allowlist.

    Matching key is the ``(provider, subject_id)`` pair. ``subject_id`` is
    the immutable IdP-side identifier (GitHub numeric ID, Google OIDC ``sub``
    claim) — never the username/email, which can change at the IdP without
    breaking the match. ``subject_label`` is a display-only snapshot taken
    at allowlist-add time; it is NEVER used for matching.

    Migration window note (#655): ``github_user_id``/``github_username`` are
    kept NOT NULL alongside the new provider-aware columns so the existing
    GitHub admin-API path (``add_to_allowlist(github_username=...)``) can
    continue to populate them without DDL churn. Physical drop is deferred
    to a future issue once all admin tooling has switched to ``subject_id``.
    """

    __tablename__ = "signup_allowlist"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    # --- Provider-aware matching keys (#655) ---
    # ``provider`` widens the natural key from (github_user_id, source) to
    # (provider, subject_id, source). server_default lets existing GitHub
    # admin tooling INSERT without specifying provider explicitly.
    provider: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'github'"),
    )
    # ``subject_id`` holds the immutable IdP identity: numeric GitHub user
    # ID for github rows, OIDC ``sub`` claim for google rows. Sized 255 to
    # accommodate either (GitHub IDs are <10 digits, Google subs are ~21
    # digits, plus headroom for any future provider quirks).
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # ``subject_label`` is display-only: GitHub login for github rows,
    # email for google rows. NEVER used for the allowlist match — matching
    # on a mutable IdP field would re-open the email-change attack the
    # provider-aware design was added to close.
    subject_label: Mapped[str] = mapped_column(String(255), nullable=False)

    # --- Legacy GitHub-only columns (deprecated #655, retained NOT NULL
    # during the migration window so existing admin paths still write them
    # alongside the new provider-aware columns; physical drop deferred) ---
    github_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    github_username: Mapped[str] = mapped_column(String(255), nullable=False)

    source: Mapped[str] = mapped_column(String(32), nullable=False, server_default="manual")
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    added_by_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- Phase 2 (reserved — populated by Sponsors sync later) ---
    sponsor_tier_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    grace_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "provider IN ('github', 'google')",
            name="valid_signup_allowlist_provider",
        ),
        CheckConstraint(
            "source IN ('manual', 'github_sponsors')",
            name="valid_signup_allowlist_source",
        ),
        CheckConstraint(
            "state IN ('active', 'grace', 'revoked')",
            name="valid_signup_allowlist_state",
        ),
        # A single subject can legitimately sit in both ``manual`` and
        # ``github_sponsors`` sources simultaneously (admin whitelisted them
        # AND they later became a sponsor). (provider, subject_id, source)
        # is the natural key in the post-#655 model.
        UniqueConstraint(
            "provider",
            "subject_id",
            "source",
            name="uq_allowlist_provider_subject_source",
        ),
        Index(
            "ix_signup_allowlist_provider_subject",
            "provider",
            "subject_id",
        ),
    )
