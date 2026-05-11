"""Signup gate SQLAlchemy models (Issue #358).

Phase 1 of admin-configurable signup gate. The single-row
``signup_gate_config`` holds the runtime switch + mode; ``signup_allowlist``
keys each allowed registrant on the immutable numeric ``github_user_id``
(renames don't break the match).

Sponsors-related columns on ``SignupGateConfig`` are reserved in Phase 1 and
populated by Phase 2's GraphQL sync + webhook flow.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
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

    Match is on ``github_user_id`` (immutable numeric ID), never on
    ``github_username`` — GitHub usernames can be renamed and the rename
    leaves an existing allowlist row pointing at the same person.
    """

    __tablename__ = "signup_allowlist"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    github_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
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
            "source IN ('manual', 'github_sponsors')",
            name="valid_signup_allowlist_source",
        ),
        CheckConstraint(
            "state IN ('active', 'grace', 'revoked')",
            name="valid_signup_allowlist_state",
        ),
        # Same GitHub user can legitimately sit in both `manual` and
        # `github_sponsors` sources simultaneously (admin whitelisted them AND
        # they later became a sponsor). (github_user_id, source) is the key.
        UniqueConstraint("github_user_id", "source", name="uq_allowlist_user_source"),
    )
