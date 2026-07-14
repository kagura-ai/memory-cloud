"""Agent correlation context (RFC-0002 P0-4, Issue #1277).

Parses W3C Trace Context (``traceparent``) + ``baggage`` into a per-request
contextvar so audit/usage writers can stamp joinable correlation identifiers
(``agent_id``, ``session_id``, ``run_id``, ``trace_id``, ``span_id``) without
threading parameters through handler signatures — a sibling of the #963
MCP key-workspace-scope contextvar (design F4,
``docs/design/agent-otel-correlation.md``).

**Correlation is observability, never authorization.** Headers/baggage are
advisory: nothing here grants or denies access. Invalid values are dropped
(never a request failure); missing ``traceparent`` triggers server-side ID
generation so every audit row is correlatable.

Identity precedence (normative): credential-bound ``agent_id`` (verified key)
> explicit bootstrap arg > baggage claim. A claim never outranks a credential
— see :func:`resolve_agent_correlation`.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from utils.logger import get_logger

logger = get_logger(__name__)

# OTel GenAI baggage keys (published attribute names; vendor key for run).
BAGGAGE_AGENT_ID = "gen_ai.agent.id"
BAGGAGE_SESSION_ID = "gen_ai.conversation.id"
BAGGAGE_SESSION_ID_ALIAS = "session.id"
BAGGAGE_RUN_ID = "kagura.agent.run.id"

# policy_decision stamped on rows whose agent_id came from a *verified baggage
# claim* on an agent-UNBOUND credential (attribution without containment).
POLICY_DECISION_UNBOUND = "unbound"

_CORRELATION_TOKEN_MAX_LEN = 128
_CORRELATION_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# traceparent: version(2)-trace_id(32)-parent_id(16)-flags(2), all lowercase hex.
_TRACEPARENT_RE = re.compile(
    r"^(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)


@dataclass
class CorrelationContext:
    """Per-request correlation identifiers (all optional except the always-set
    server-generated trace/span)."""

    trace_id: str
    span_id: str
    session_id: str | None = None
    run_id: str | None = None
    # Raw baggage agent-id claim (unresolved). Resolution to the audit
    # ``agent_id`` column + policy_decision happens once the credential
    # identity is known (resolve_agent_correlation).
    agent_claim: str | None = None
    # 'mcp' | 'rest' — the request surface, set at the transport/middleware
    # seam so service-layer audit emission (#1278) is surface-invariant.
    surface: str = "rest"


_correlation: ContextVar[CorrelationContext | None] = ContextVar("correlation", default=None)


def set_correlation(ctx: CorrelationContext | None) -> None:
    _correlation.set(ctx)


def get_correlation() -> CorrelationContext | None:
    return _correlation.get()


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------


def validate_correlation_token(value: Any) -> str | None:
    """Validate an opaque ``session_id`` / ``run_id`` token; None if invalid.

    Charset ``[A-Za-z0-9._-]``, length ≤128 (design F4). Invalid = dropped
    (advisory data, never a request failure).
    """
    if value is None:
        # Absent baggage key (bag.get(...) -> None) is the overwhelmingly common
        # case, not an anomaly — drop silently. Only a *present* malformed value
        # warrants the structured drop warning below (Copilot review, #1277).
        return None
    if not isinstance(value, str) or not value or len(value) > _CORRELATION_TOKEN_MAX_LEN:
        # Structured warning on drop (design F4 §validation) — advisory, never
        # a request failure. Never log the token verbatim (it may, despite the
        # opacity contract, carry client PII); log only its length/type.
        logger.warning(
            "correlation_token_dropped",
            reason="length_or_type",
            length=len(value) if isinstance(value, str) else None,
            value_type=type(value).__name__,
        )
        return None
    if not _CORRELATION_TOKEN_RE.match(value):
        logger.warning("correlation_token_dropped", reason="charset", length=len(value))
        return None
    return value


def parse_traceparent(header: str | None) -> tuple[str, str] | None:
    """Parse a W3C ``traceparent`` header → ``(trace_id, span_id)`` or None.

    Rejects the all-zero trace/span ids (invalid per the spec).
    """
    if not header:
        return None
    m = _TRACEPARENT_RE.match(header.strip())
    if not m:
        # Advisory: a malformed traceparent (e.g. from a misconfigured proxy)
        # is dropped and the server generates its own ids — but log it so the
        # misconfiguration is diagnosable (design F4 §validation).
        logger.warning("traceparent_invalid", reason="malformed")
        return None
    trace_id, span_id = m.group("trace_id"), m.group("span_id")
    if trace_id == "0" * 32 or span_id == "0" * 16:
        logger.warning("traceparent_invalid", reason="all_zero_id")
        return None
    return trace_id, span_id


def parse_baggage(header: str | None) -> dict[str, str]:
    """Parse a W3C ``baggage`` header into a flat ``{key: value}`` dict.

    Best-effort: malformed entries are skipped, per-entry properties (after
    ``;``) are ignored. Never raises.
    """
    result: dict[str, str] = {}
    if not header:
        return result
    for entry in header.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        key, _, value = entry.partition("=")
        # Strip any per-member properties after ';'.
        value = value.split(";", 1)[0].strip()
        key = key.strip()
        if key and value:
            result[key] = value
    return result


def generate_trace_id() -> str:
    """Generate a random 32-hex W3C trace-id (server-side, when none arrives)."""
    import secrets

    return secrets.token_hex(16)


def generate_span_id() -> str:
    """Generate a random 16-hex W3C span-id."""
    import secrets

    return secrets.token_hex(8)


def build_correlation_from_headers(
    *,
    traceparent: str | None,
    baggage: str | None,
    surface: str = "rest",
) -> CorrelationContext:
    """Assemble the per-request CorrelationContext from raw headers.

    - ``trace_id``/``span_id`` from ``traceparent`` when valid, else generated.
    - ``session_id``/``run_id`` from baggage after token validation (invalid →
      dropped).
    - ``agent_claim`` = the raw baggage ``gen_ai.agent.id`` (unresolved).
    """
    tp = parse_traceparent(traceparent)
    if tp is not None:
        trace_id, span_id = tp
    else:
        trace_id, span_id = generate_trace_id(), generate_span_id()

    bag = parse_baggage(baggage)
    session_id = validate_correlation_token(
        bag.get(BAGGAGE_SESSION_ID) or bag.get(BAGGAGE_SESSION_ID_ALIAS)
    )
    run_id = validate_correlation_token(bag.get(BAGGAGE_RUN_ID))
    agent_claim = bag.get(BAGGAGE_AGENT_ID) or None

    return CorrelationContext(
        trace_id=trace_id,
        span_id=span_id,
        session_id=session_id,
        run_id=run_id,
        agent_claim=agent_claim,
        surface=surface,
    )


# ---------------------------------------------------------------------------
# Identity precedence + claim verification (normative, design F4)
# ---------------------------------------------------------------------------


@dataclass
class ResolvedAgentCorrelation:
    """Outcome of applying the identity-precedence rules to a request."""

    agent_id: UUID | None  # what an audit row's agent_id column should hold
    policy_decision: str | None  # 'unbound' for verified-claim-on-unbound-cred
    unverified_agent_claim_hash: str | None  # keyed hash, never verbatim
    correlation_conflict: bool  # explicit arg disagreed with baggage (unbound cred)


def _hash_claim(claim: str) -> str:
    from config.settings import get_settings
    from utils.hashing import hmac_sha256_hex

    # audit_hmac_key is a required Settings field with a non-empty default —
    # use it directly (utils.hashing's contract: never a hard-coded constant).
    return hmac_sha256_hex(claim, get_settings().audit_hmac_key)


async def verify_baggage_agent_claim(
    db: Any, *, claimed_agent_id: UUID, member_user_id: str, workspace_id: UUID
) -> bool:
    """Rule 2 predicate: a baggage ``gen_ai.agent.id`` claim verifies IFF the
    claimed agent is bound (via ``api_keys.agent_id``) to the SAME member row
    as the authenticated credential, in the same workspace.

    Confines attribution to agents demonstrably operated by the same
    authenticated service member — the answer to claim-forgery, not an
    instance of it.
    """
    from sqlalchemy import select

    from models.agent import Agent
    from models.auth import APIKey

    # The claimed agent must exist in the workspace ...
    agent = (
        await db.execute(
            select(Agent.id).where(Agent.id == claimed_agent_id, Agent.workspace_id == workspace_id)
        )
    ).scalar_one_or_none()
    if agent is None:
        return False
    # ... and the authenticated member must hold at least one non-revoked
    # api_key bound to that agent (same-member proof).
    key = (
        await db.execute(
            select(APIKey.id).where(
                APIKey.agent_id == claimed_agent_id,
                APIKey.user_id == member_user_id,
                APIKey.revoked_at.is_(None),
            )
        )
    ).first()
    return key is not None


async def resolve_agent_correlation(
    db: Any,
    *,
    credential_agent_id: UUID | None,
    explicit_agent_id: UUID | None,
    member_user_id: str | None,
    workspace_id: UUID | None,
    correlation: CorrelationContext | None = None,
) -> ResolvedAgentCorrelation:
    """Apply the normative precedence to determine the audit ``agent_id``.

    Precedence: credential-bound ``agent_id`` > explicit arg > baggage claim.

    - **Credential-bound** (Rule 1): the credential wins unconditionally; any
      disagreeing explicit arg / baggage claim is recorded ONLY as a keyed-hash
      ``unverified_agent_claim`` — never precedence-resolved in favor of the
      claim.
    - **Agent-unbound credential** (Rules 2/5): an explicit arg wins over
      baggage (``correlation_conflict`` when they disagree); a baggage claim is
      trusted into ``agent_id`` IFF :func:`verify_baggage_agent_claim` passes,
      and such rows are stamped ``policy_decision='unbound'``. An unverified
      claim is keyed-hashed only.
    """
    baggage_claim_raw = correlation.agent_claim if correlation else None

    # --- Credential-bound: Rule 1 controls unconditionally. --------------
    if credential_agent_id is not None:
        disagreeing: str | None = None
        if explicit_agent_id is not None and explicit_agent_id != credential_agent_id:
            disagreeing = str(explicit_agent_id)
        elif baggage_claim_raw and baggage_claim_raw != str(credential_agent_id):
            disagreeing = baggage_claim_raw
        return ResolvedAgentCorrelation(
            agent_id=credential_agent_id,
            policy_decision=None,
            unverified_agent_claim_hash=_hash_claim(disagreeing) if disagreeing else None,
            correlation_conflict=False,
        )

    # --- Agent-unbound credential. ---------------------------------------
    if member_user_id is None or workspace_id is None:
        # Cannot verify anything — keep any raw claim as a hash only.
        return ResolvedAgentCorrelation(
            agent_id=None,
            policy_decision=None,
            unverified_agent_claim_hash=_hash_claim(baggage_claim_raw)
            if baggage_claim_raw
            else None,
            correlation_conflict=False,
        )

    # Rule 5: explicit arg wins over baggage; conflict flagged when they differ.
    conflict = bool(
        explicit_agent_id is not None
        and baggage_claim_raw
        and baggage_claim_raw != str(explicit_agent_id)
    )
    candidate = explicit_agent_id
    candidate_raw = str(explicit_agent_id) if explicit_agent_id is not None else baggage_claim_raw

    if candidate is None:
        # Only a baggage claim — parse it to a UUID.
        candidate = _coerce_uuid(baggage_claim_raw)
    if candidate is None:
        return ResolvedAgentCorrelation(
            agent_id=None,
            policy_decision=None,
            unverified_agent_claim_hash=_hash_claim(candidate_raw) if candidate_raw else None,
            correlation_conflict=conflict,
        )

    verified = await verify_baggage_agent_claim(
        db, claimed_agent_id=candidate, member_user_id=member_user_id, workspace_id=workspace_id
    )
    if verified:
        # Attribution without containment — stamp 'unbound'.
        return ResolvedAgentCorrelation(
            agent_id=candidate,
            policy_decision=POLICY_DECISION_UNBOUND,
            unverified_agent_claim_hash=None,
            correlation_conflict=conflict,
        )
    # Rule 3: unverified claim never reaches agent_id — keyed hash only.
    return ResolvedAgentCorrelation(
        agent_id=None,
        policy_decision=None,
        unverified_agent_claim_hash=_hash_claim(candidate_raw) if candidate_raw else None,
        correlation_conflict=conflict,
    )


def _coerce_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None
