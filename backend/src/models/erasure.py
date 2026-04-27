"""SQLAlchemy model for GDPR right-to-erasure workflow (Issue #360).

`erasure_requests` row is the single source of truth for an account-deletion
request lifecycle: ``pending → cooling_off → in_progress → complete`` (happy
path) with ``cancelled`` and ``failed`` as terminal off-ramps. Admin force-
erase skips ``cooling_off`` and goes directly to ``in_progress``.

The row outlives the user being erased — it stays for 5 years as
pseudonymized accountability evidence (GDPR Art.5(1)(b) / Art.30). After
``complete``, the foreign user_id no longer exists in ``users`` because the
service deletes the user row itself; readers must therefore not assume
``erasure_requests.user_id`` joins to a live ``users.user_id``.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from db.base import Base

# Status values — kept in sync with the CHECK constraint in
# alembic/versions/c01_360_erasure_requests.py and the AccountErasureService
# state machine. Do not add values here without updating both.
STATUS_PENDING = "pending"
STATUS_COOLING_OFF = "cooling_off"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

VALID_STATUSES = frozenset(
    {
        STATUS_PENDING,
        STATUS_COOLING_OFF,
        STATUS_IN_PROGRESS,
        STATUS_COMPLETE,
        STATUS_FAILED,
        STATUS_CANCELLED,
    }
)

# Reason codes — admin paths use the non-self_service values; the
# self-service path is locked to ``self_service`` by the CHECK constraint.
REASON_SELF_SERVICE = "self_service"
REASON_USER_REQUEST_VIA_SUPPORT = "user_request_via_support"
REASON_LEGAL_ORDER = "legal_order"
REASON_INACTIVITY_POLICY = "inactivity_policy"
REASON_ABUSE_VIOLATION = "abuse_violation"
REASON_OTHER = "other"

VALID_REASON_CODES = frozenset(
    {
        REASON_SELF_SERVICE,
        REASON_USER_REQUEST_VIA_SUPPORT,
        REASON_LEGAL_ORDER,
        REASON_INACTIVITY_POLICY,
        REASON_ABUSE_VIOLATION,
        REASON_OTHER,
    }
)

# App-layer cap on reason_detail. Kept in sync with API request schema.
REASON_DETAIL_MAX_CHARS = 1000


class ErasureRequest(Base):
    """Account-erasure workflow row (Issue #360, GDPR Art.17 / APPI 第22条).

    See module docstring for lifecycle. The ``user_id`` is the OAuth2 sub
    string used everywhere as the universal cross-table key, NOT a FK.
    """

    __tablename__ = "erasure_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())

    # Subject of the erasure (OAuth2 sub). Not a FK — repo convention.
    # Indexed via the explicit Index() entry in __table_args__ below so the
    # index has a deterministic name and matches the migration shape.
    user_id = Column(String(255), nullable=False)

    # SHA256 of the user's email at request time. Plaintext email never on disk.
    user_email_hash = Column(String(64), nullable=False)

    # Self user_id (self-service) or admin user_id (admin force-erase).
    initiated_by = Column(String(255), nullable=False)

    is_self_service = Column(Boolean, nullable=False, default=False)

    reason_code = Column(String(50), nullable=False)
    reason_detail = Column(Text, nullable=True)

    status = Column(String(20), nullable=False, default=STATUS_PENDING)

    # SHA256 of the one-time token; raw token in Redis only.
    confirm_token_hash = Column(String(128), nullable=True)

    requested_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    scheduled_for = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)

    failure_reason = Column(Text, nullable=True)
    deleted_data_summary = Column(JSONB, nullable=True)

    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(255), nullable=True)

    # Source the CHECK constraints from VALID_STATUSES / VALID_REASON_CODES
    # so adding a new state value never requires editing two places. The
    # migration file at c01_360_erasure_requests.py is a frozen snapshot
    # and intentionally hardcodes these literals — that's the alembic
    # convention. The runtime model stays in sync with the constants.
    _STATUS_LITERALS = ", ".join(f"'{s}'" for s in sorted(VALID_STATUSES))
    _REASON_LITERALS = ", ".join(f"'{r}'" for r in sorted(VALID_REASON_CODES))

    __table_args__ = (
        CheckConstraint(
            f"status IN ({_STATUS_LITERALS})",
            name="valid_erasure_status",
        ),
        CheckConstraint(
            f"reason_code IN ({_REASON_LITERALS})",
            name="valid_erasure_reason_code",
        ),
        CheckConstraint(
            f"(is_self_service = true AND reason_code = '{REASON_SELF_SERVICE}') OR "
            f"(is_self_service = false AND reason_code <> '{REASON_SELF_SERVICE}')",
            name="erasure_self_service_reason_consistency",
        ),
        Index("ix_erasure_requests_user_id", "user_id"),
        Index(
            "ix_erasure_requests_pending_sweep",
            "scheduled_for",
            postgresql_where=text(f"status = '{STATUS_COOLING_OFF}'"),
        ),
        Index("ix_erasure_requests_status", "status"),
        Index(
            "uq_erasure_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text(
                f"status IN ('{STATUS_PENDING}', '{STATUS_COOLING_OFF}', '{STATUS_IN_PROGRESS}')"
            ),
        ),
    )

    def __repr__(self) -> str:
        return f"<ErasureRequest(id={self.id}, user_id='{self.user_id}', status='{self.status}')>"
