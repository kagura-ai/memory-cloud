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
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from config.settings import get_settings
from models.public_mcp_domain import (
    CHECK_TIMEOUT_SECONDS,
    PROOF_GRACE_HOURS,
    PROOF_MIN_MISSING_CHECKS,
    SUSPENDED_HOLD_DAYS,
)
from services import public_mcp_domain_dns as domain_dns
from services.public_mcp_domain_dns import DomainCheck, ProofResult, RoutingResult
from utils.logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "AuditAction",
    "Decision",
    "DomainSnapshot",
    "decide_check",
    "lookup",
]

DomainState = Literal["pending", "active", "suspended", "released"]
SuspendedReason = Literal["proof_lost", "right_ended", "reserved"]
AuditAction = Literal["attached", "verified", "suspended", "reinstated", "detached", "released"]

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
