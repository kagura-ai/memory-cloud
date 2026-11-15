"""Admin read of the audit log (Issue #2835).

``GET /api/v1/admin/audit-logs`` — the ``audit_logs`` rows that admin actions
and sign-up decisions write, newest first, behind the system-admin gate in
every ``SYSTEM_ADMIN_MODE`` (``AdminUser``: a system admin in ``session``
mode, an operator principal in ``service`` mode; ``off`` is 404).

Three constants decide what the route answers. Adding to any of them is a
reviewed change:

- ``AUDIT_LOG_ACTIONS``: the actions the route returns. Every query carries
  them (``_action_predicate``), narrowed by the ``action`` parameter.
- ``PLAIN_VALUE_ACTIONS``: the actions whose writer stores plain values in
  the two value columns; only they answer ``old_value`` / ``new_value``.
- ``AUDIT_METADATA_KEYS``: per action, the ``user_metadata`` keys answered.

The route only reads: one session, no write, no audit row for the read.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, get_args

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime, BeforeValidator, ConfigDict
from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.admin_sleep import SLEEP_RUN_TRIGGER_AUDIT_ACTION
from auth.dependencies import AdminUser
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.auth import AuditLog
from services.sleep.undo import SLEEP_MERGE_UNDO_AUDIT_ACTION
from utils.exceptions import ValidationError
from utils.logger import get_logger
from utils.query_bounds import MAX_INT32, NO_NUL_PATTERN

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/audit-logs", tags=["admin"])

AuditLogAction = Literal[
    "system_role_update",
    "system_admin_promote",
    "system_admin_demote",
    "workspace_slot_bonus_update",
    "referral_grant_revoke",
    "workspace_ownership_force_transfer",
    "workspace_ownership_transfer",
    "workspace_purged",
    "account_erasure",
    "context_restore",
    "bm25_drift_reveal_terms",
    "mcp_notice.created",
    "mcp_notice.withdrawn",
    "signup_blocked",
    "signup_admitted",
    "beta_invite.redeemed",
    # #2838: the run and the merge undo an admin or operator starts for a user
    # (``SLEEP_RUN_TRIGGER_AUDIT_ACTION``, ``SLEEP_MERGE_UNDO_AUDIT_ACTION``).
    "sleep_run_trigger",
    "sleep_merge_undo",
]

# The actions this route returns: admin actions and sign-up decisions.
AUDIT_LOG_ACTIONS: frozenset[str] = frozenset(get_args(AuditLogAction))

# The ids the two Sleep writers store; neither stores a value column.
_SLEEP_RUN_KEYS: frozenset[str] = frozenset(
    {"target_user_id", "workspace_id", "context_id", "report_id"}
)

# The actions whose writer stores plain values (roles, numbers) in
# ``old_value_hash`` / ``new_value_hash``.
PLAIN_VALUE_ACTIONS: frozenset[str] = frozenset(
    {
        "system_role_update",
        "system_admin_promote",
        "system_admin_demote",
        "workspace_slot_bonus_update",
        "referral_grant_revoke",
    }
)

# The ``user_metadata`` keys answered, per action: what its writer stores.
AUDIT_METADATA_KEYS: dict[str, frozenset[str]] = {
    "system_role_update": frozenset({"actor_user_id", "target_user_id", "old_role", "new_role"}),
    "system_admin_promote": frozenset({"promoted_by", "is_initial_admin"}),
    "system_admin_demote": frozenset({"demoted_by", "was_initial_admin"}),
    "workspace_slot_bonus_update": frozenset(
        {"actor_user_id", "target_user_id", "before_value", "after_value", "delta", "reason"}
    ),
    "referral_grant_revoke": frozenset(
        {
            "actor_user_id",
            "referrer_user_id",
            "referred_user_id",
            "referrer_bonus_memories",
            "referred_bonus_memories",
            "reason",
        }
    ),
    "workspace_ownership_force_transfer": frozenset(
        {
            "break_glass",
            "reason",
            "previous_owner_id",
            "new_owner_id",
            "ownership_epoch",
            "target_was_member",
            "self_transfer",
            "dual_control",
            "force_transfer_request_id",
            "initiated_by_user_id",
            "initiated_by_email",
            "approved_by_user_id",
        }
    ),
    "workspace_ownership_transfer": frozenset(
        {"previous_owner_id", "new_owner_id", "ownership_epoch"}
    ),
    "workspace_purged": frozenset(
        {"previous_owner_id", "soft_deleted", "contexts", "memories", "billing_contract_active"}
    ),
    "account_erasure": frozenset({"request_id", "is_self_service", "initiated_by", "reason_code"}),
    "context_restore": frozenset(
        {
            "workspace_id",
            "name",
            "renamed_from",
            "deleted_at",
            "deleted_by",
            "memories_restored",
            "memories_left_deleted",
            "files_restored",
            "file_bytes_restored",
        }
    ),
    "bm25_drift_reveal_terms": frozenset(
        {"outcome", "context_id", "reason", "num_terms_resolved", "num_terms_total"}
    ),
    "mcp_notice.created": frozenset({"text_chars", "starts_at", "expires_at", "plans"}),
    "mcp_notice.withdrawn": frozenset({"text_chars"}),
    "signup_blocked": frozenset({"provider"}),
    "signup_admitted": frozenset({"provider", "admission", "invitations", "invitation_count"}),
    "beta_invite.redeemed": frozenset({"provider", "inviter_user_id", "allowlist_entry_id"}),
    SLEEP_RUN_TRIGGER_AUDIT_ACTION: _SLEEP_RUN_KEYS,
    SLEEP_MERGE_UNDO_AUDIT_ACTION: _SLEEP_RUN_KEYS
    | {"action_id", "restored_memory_id", "winner_id"},
}

# The two listed keys that hold a list.
_PLANS_KEY = "plans"
_INVITATIONS_KEY = "invitations"
_INVITATION_KEYS = ("id", "workspace_id", "invited_by")


class AuditLogEntry(TZAwareBaseModel):
    """One audit row."""

    model_config = ConfigDict(extra="forbid")

    id: int
    created_at: datetime
    action: str
    user_id: str
    user_email: str
    resource: str
    old_value: str | None
    new_value: str | None
    metadata: dict[str, Any] | None


class AuditLogListResponse(TZAwareBaseModel):
    """A page of audit rows and the cursor of the next page."""

    model_config = ConfigDict(extra="forbid")

    entries: list[AuditLogEntry]
    next_before_id: int | None


def _invitation(entry: dict[str, Any]) -> dict[str, str]:
    """An ``invitations`` entry reduced to its three string keys."""
    return {
        key: entry[key] for key in _INVITATION_KEYS if key in entry and isinstance(entry[key], str)
    }


def audit_metadata_of(action: str, stored: Any) -> dict[str, Any] | None:
    """The stored ``user_metadata`` through the action's key list.

    Args:
        action: The row's action.
        stored: The row's ``user_metadata`` (a dict, or None).

    Returns:
        None when nothing or no object is stored. Else the keys listed for
        the action in ``AUDIT_METADATA_KEYS`` whose value is a string, a
        number, a boolean or null; ``plans`` also as a list of strings, and
        ``invitations`` as a list of objects reduced to ``id``,
        ``workspace_id`` and ``invited_by``. Every other key, object or list
        is left out.
    """
    if not isinstance(stored, dict):
        return None
    listed = AUDIT_METADATA_KEYS.get(action, frozenset())
    answered: dict[str, Any] = {}
    for key, value in stored.items():
        if key not in listed:
            continue
        if value is None or isinstance(value, str | int | float | bool):
            answered[key] = value
        elif key == _PLANS_KEY and isinstance(value, list):
            if all(isinstance(plan, str) for plan in value):
                answered[key] = list(value)
        elif key == _INVITATIONS_KEY and isinstance(value, list):
            answered[key] = [_invitation(entry) for entry in value if isinstance(entry, dict)]
    return answered


def _action_predicate(asked: Collection[str] | None) -> ColumnElement[bool]:
    """The one place the ``action`` condition is built: never absent.

    Without ``asked`` it is every listed action; with it, the listed actions
    that were asked for.
    """
    actions = AUDIT_LOG_ACTIONS if asked is None else AUDIT_LOG_ACTIONS.intersection(asked)
    return AuditLog.action.in_(sorted(actions))


def _iso_8601(value: Any) -> Any:
    """Read a query value as an ISO 8601 date and time (the zone is checked by the type).

    A time whose UTC value falls outside the calendar (year 1 with a positive
    offset, year 9999 with a negative one) is refused here: ``created_at`` is
    compared in UTC.
    """
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                parsed.astimezone(UTC)
        except (ValueError, OverflowError):
            raise ValueError("an ISO 8601 date and time with a zone is required") from None
        return parsed
    return value


ZonedDatetime = Annotated[AwareDatetime, BeforeValidator(_iso_8601)]


def _naive_utc(value: datetime) -> datetime:
    """An aware datetime as the naive UTC value ``created_at`` stores."""
    return value.astimezone(UTC).replace(tzinfo=None)


class AuditLogRangeError(ValidationError):
    """``from`` is later than ``to`` (422)."""

    log_level = "warning"

    def __init__(self) -> None:
        super().__init__("'from' must not be later than 'to'", field="from")


# The columns the read selects. The two value columns are answered for
# ``PLAIN_VALUE_ACTIONS`` only, ``user_metadata`` through ``audit_metadata_of``.
_COLUMNS = (
    AuditLog.id,
    AuditLog.created_at,
    AuditLog.action,
    AuditLog.user_id,
    AuditLog.user_email,
    AuditLog.resource,
    AuditLog.old_value_hash,
    AuditLog.new_value_hash,
    AuditLog.user_metadata,
)


def _entry(row: Any) -> AuditLogEntry:
    """Build one entry from a row of ``_COLUMNS``."""
    plain = row.action in PLAIN_VALUE_ACTIONS
    return AuditLogEntry(
        id=row.id,
        created_at=row.created_at,
        action=row.action,
        user_id=row.user_id,
        user_email=row.user_email,
        resource=row.resource,
        old_value=row.old_value_hash if plain else None,
        new_value=row.new_value_hash if plain else None,
        metadata=audit_metadata_of(row.action, row.user_metadata),
    )


@router.get(
    "",
    response_model=AuditLogListResponse,
    summary="Read the audit log of admin actions and sign-up decisions",
)
async def list_audit_logs(
    admin: AdminUser,
    action: list[AuditLogAction] | None = Query(
        default=None, description="Repeatable. Only the listed actions; another value is a 422."
    ),
    user_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=255,
        pattern=NO_NUL_PATTERN,
        description="Exact match on the stored value.",
    ),
    user_email: str | None = Query(
        default=None,
        min_length=1,
        max_length=255,
        pattern=NO_NUL_PATTERN,
        description="Exact match on the stored value.",
    ),
    resource: str | None = Query(
        default=None,
        min_length=1,
        max_length=255,
        pattern=NO_NUL_PATTERN,
        description="Exact match on the stored value.",
    ),
    from_: ZonedDatetime | None = Query(
        default=None,
        alias="from",
        description="Rows created at or after this time. ISO 8601 with a zone.",
    ),
    to: ZonedDatetime | None = Query(
        default=None, description="Rows created before this time. ISO 8601 with a zone."
    ),
    limit: int = Query(default=50, ge=1, le=100),
    before_id: int | None = Query(
        default=None, ge=1, le=MAX_INT32, description="Cursor: rows with a smaller `id`."
    ),
    db: AsyncSession = Depends(get_db),
) -> AuditLogListResponse:
    """The audit rows of admin actions and sign-up decisions, newest first.

    The filters combine with AND. Page with `before_id`: pass the
    `next_before_id` of the previous answer; it is null on the last page.

    Each entry answers `id`, `created_at` (UTC), `action`, `user_id`,
    `user_email` and `resource` as stored, so a row shows what an account
    erasure left of it. In `service` mode an actor is stored as
    `operator:<address>`.

    | `action` | `user_id` | `user_email` | `resource` |
    |---|---|---|---|
    | `system_role_update` | the actor | the actor | `user:<target id>` |
    | `system_admin_promote`, `system_admin_demote` | the target | the actor | `user:<target address>` |
    | `workspace_slot_bonus_update` | the actor | the actor | `user:<target address>` |
    | `referral_grant_revoke` | the actor | the actor | `referral_grant:<id>` |
    | `workspace_ownership_force_transfer` | the actor | the actor | `workspace:<id>` |
    | `workspace_ownership_transfer` | the actor: the owner handing on, an admin or a CLI run | the actor | `workspace:<id>` |
    | `workspace_purged` | the actor or the owner | the actor | `workspace:<id>` |
    | `account_erasure` | the subject's pseudonym | the pseudonym of the subject's address | `user_pseudonym:<prefix>` |
    | `context_restore` | the actor | the actor | `context:<id>` |
    | `bm25_drift_reveal_terms` | the actor | the actor | `bm25_drift_log:<id>` |
    | `mcp_notice.created`, `mcp_notice.withdrawn` | the actor | the actor | `mcp_notice:<id>` |
    | `signup_blocked` | the subject | `oauth-callback` | `signup_gate:<provider>` |
    | `signup_admitted` | the subject | the sign-up path | `signup_gate:<provider>` |
    | `beta_invite.redeemed` | the subject | `email-signup` or `oauth-callback` | `beta_invite:<id>` |
    | `sleep_run_trigger` | the actor | the actor | `context:<id>` |
    | `sleep_merge_undo` | the actor | the actor | `sleep_report:<id>` |

    `old_value` and `new_value` are answered for `system_role_update`,
    `system_admin_promote`, `system_admin_demote`,
    `workspace_slot_bonus_update` and `referral_grant_revoke`, and are null
    for every other action. `metadata` holds the keys listed for the row's
    action.
    """
    if from_ is not None and to is not None and from_ > to:
        raise AuditLogRangeError()

    conditions: list[ColumnElement[bool]] = [_action_predicate(action)]
    if user_id is not None:
        conditions.append(AuditLog.user_id == user_id)
    if user_email is not None:
        conditions.append(AuditLog.user_email == user_email)
    if resource is not None:
        conditions.append(AuditLog.resource == resource)
    if from_ is not None:
        conditions.append(AuditLog.created_at >= _naive_utc(from_))
    if to is not None:
        conditions.append(AuditLog.created_at < _naive_utc(to))
    if before_id is not None:
        conditions.append(AuditLog.id < before_id)

    rows = (
        await db.execute(
            select(*_COLUMNS).where(*conditions).order_by(AuditLog.id.desc()).limit(limit + 1)
        )
    ).all()
    entries = [_entry(row) for row in rows[:limit]]
    next_before_id = entries[-1].id if len(rows) > limit else None

    logger.info(
        "audit_logs_listed",
        admin_user=admin["user_id"],
        count=len(entries),
        limit=limit,
        actions=sorted(set(action)) if action is not None else None,
        user_id_filter=user_id is not None,
        user_email_filter=user_email is not None,
        resource_filter=resource is not None,
        from_set=from_ is not None,
        to_set=to is not None,
        before_id_set=before_id is not None,
    )
    return AuditLogListResponse(entries=entries, next_before_id=next_before_id)
