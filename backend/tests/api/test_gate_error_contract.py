"""The gate error contract (#1644), enumerated in one place.

Every plan or quota refusal the API can emit carries a machine-readable
``details`` block: ``gate`` says WHY the request was refused (a plan, a
quota, a rollout allowlist, a deployment switch), and a quota refusal adds
``quota_type`` plus canonical ``current`` / ``limit`` counts. ``gate`` is an
ADDITIVE annotation, orthogonal to ``error`` and ``status``: a client that
ignores it sees the same status, the same message and the same pre-existing
detail fields it always did.

The contract is only worth anything if it is enforced over the WHOLE set of
refusals, so this module has three layers:

1. ``REFUSALS`` enumerates every gate refusal with the exception the site
   actually produces, built through the same registry helpers the site calls.
   The per-field assertions run over that table.
2. ``test_the_enumeration_covers_every_refusal_site`` walks the AST of
   ``backend/src`` and fails when a refusal site exists that is neither in
   the table nor in ``NOT_GATE_REFUSALS``. A new refusal cannot be added
   without deciding, in this file, which vocabulary it belongs to.
3. ``test_each_enumerated_refusal_still_matches_its_site`` re-reads each
   site and fails if the discriminator moved — so the table cannot rot into
   a description of code that no longer exists.

The per-site behaviour (which raise fires when) belongs to the suites that
own those gates; this file owns the VOCABULARY and the back-compat promise.
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from api.middleware.rate_limit import RateLimitMiddleware
from config.constants import (
    GATE_ALLOWLIST,
    GATE_DEPLOYMENT,
    GATE_KINDS,
    GATE_PLAN,
    GATE_QUOTA,
    GATE_ROLE,
    MAX_CONTENT_SIZE,
    QUOTA_TYPES,
)
from config.plan_tiers import (
    PLAN_TIERS,
    PlanName,
    feature_gate_details,
    lowest_tier_with_limit,
    quota_gate_details,
    required_plan_display_name,
)
from models.schemas import PatchMemoryRequest, RememberRequest, UpdateMemoryRequest
from services.connector_provisioning import ConnectorProvisioningService
from utils.exceptions import (
    AuthorizationError,
    EmbeddingSpendCapExceeded,
    FeatureNotAvailableError,
    MemoryCloudException,
    QuotaExceededError,
    ValidationError,
)

SRC_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src"


# ---------------------------------------------------------------------------
# The enumeration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Refusal:
    """One refusal the API can emit.

    Attributes:
        id: Human label used in the parametrize id.
        site: ``module:qualname`` of the function that raises it. Matched
            against the AST inventory, so a moved or deleted raise fails.
        exc: The exception as the site produces it — built through the same
            registry helpers, never with hand-written details.
        site_literal: A string constant that must still appear inside the
            raise at ``site``. The discriminator, so a renamed
            ``quota_type`` / ``feature`` fails here and not six months later
            in a client. ``None`` where the site interpolates it (the
            rate-limit family builds ``api_{mode}_daily``).
        legacy: Pre-#1644 ``details`` keys that MUST still be passed at the
            site (§1.3.2). The back-compat guard reads them off the raise's
            own keywords, so replacing one with a canonical alias fails.
        carries_counts: Whether this refusal is one that ships ``current`` /
            ``limit``. False for the two families that genuinely have no
            counts to ship (the USD spend caps and the rate-limit family);
            asserted in both directions.
    """

    id: str
    site: str
    exc: MemoryCloudException
    site_literal: str | None = None
    legacy: frozenset[str] = field(default_factory=frozenset)
    carries_counts: bool = True


_FREE = PlanName.FREE
_ANALYSIS_REFUSAL = "Memory analysis is not yet enabled for this workspace."


def _rate_limit_refusal(path: str) -> QuotaExceededError:
    """Drive the real middleware into one of its zero-limit refusals.

    The free tier has no public and no REST daily allowance, so the raise
    fires before the Redis counter is touched — the production code path,
    not a reconstruction of it.
    """
    middleware = RateLimitMiddleware(MagicMock())
    with pytest.raises(QuotaExceededError) as exc:
        asyncio.run(
            middleware._check_daily_quota(user_id="u", path=path, plan=_FREE, workspace_id=None)
        )
    return exc.value


def _connector_seat_cap() -> MemoryCloudException:
    """The real ``_raise_seat_cap`` — a staticmethod, so no DB is involved."""
    with pytest.raises(MemoryCloudException) as exc:
        ConnectorProvisioningService._raise_seat_cap(2, 2, _FREE)
    return exc.value


REFUSALS: list[Refusal] = [
    # ---- FEAT-001, gate "plan" ------------------------------------------
    Refusal(
        id="plan/team_invitations",
        site="api.routes.invitations:create_invitation",
        exc=FeatureNotAvailableError.for_feature(_FREE, "team_invitations"),
        site_literal="team_invitations",
        carries_counts=False,
    ),
    Refusal(
        id="plan/shared_contexts-rest",
        site="api.routes.contexts:create_context",
        exc=FeatureNotAvailableError.for_feature(_FREE, "shared_contexts"),
        site_literal="shared_contexts",
        carries_counts=False,
    ),
    Refusal(
        id="plan/shared_contexts-service",
        site="services.context_service:ContextService.create_context",
        exc=FeatureNotAvailableError.for_feature(_FREE, "shared_contexts"),
        site_literal="shared_contexts",
        carries_counts=False,
    ),
    Refusal(
        id="plan/public_contexts",
        site="api.routes.contexts:update_context",
        exc=FeatureNotAvailableError.for_feature(_FREE, "public_contexts"),
        site_literal="public_contexts",
        carries_counts=False,
    ),
    Refusal(
        id="plan/public_contexts-api-key",
        site="api.routes.member_credentials:create_api_key",
        exc=FeatureNotAvailableError.for_feature(_FREE, "public_contexts"),
        site_literal="public_contexts",
        carries_counts=False,
    ),
    Refusal(
        id="plan/resources",
        site="api.routes.resource_tokens:create_resource_token",
        exc=FeatureNotAvailableError.for_feature(_FREE, "resources"),
        site_literal="resources",
        carries_counts=False,
    ),
    Refusal(
        id="plan/connectors",
        site=(
            "services.connector_provisioning:"
            "ConnectorProvisioningService._enforce_connector_feature"
        ),
        exc=FeatureNotAvailableError.for_feature(_FREE, "connectors"),
        site_literal="connectors",
        carries_counts=False,
    ),
    Refusal(
        id="plan/any-feature-via-quota-service",
        site="services.quota_service:QuotaService.check_feature_access",
        exc=FeatureNotAvailableError.for_feature(_FREE, "reranking"),
        site_literal=None,
        carries_counts=False,
    ),
    Refusal(
        id="plan/memory_analysis",
        site="auth.analysis_gates:require_memory_analysis_access",
        exc=FeatureNotAvailableError(
            "memory_analysis not available",
            **feature_gate_details(None, "memory_analysis"),
        ),
        site_literal="memory_analysis",
        carries_counts=False,
    ),
    Refusal(
        id="plan/memory_analysis-mcp",
        site="auth.analysis_gates:check_memory_analysis_access_mcp",
        exc=FeatureNotAvailableError(
            "memory_analysis not available",
            **feature_gate_details(None, "memory_analysis"),
        ),
        site_literal="memory_analysis",
        carries_counts=False,
    ),
    Refusal(
        id="plan/managed_embeddings",
        site="services.embedding_service:EmbeddingService._prepare_spend_cap_gate",
        exc=FeatureNotAvailableError(
            "This workspace's plan does not include managed embeddings.",
            **feature_gate_details(_FREE, "managed_embeddings"),
        ),
        site_literal="managed_embeddings",
        carries_counts=False,
    ),
    Refusal(
        id="plan/sleep_mode",
        site="services.context_service:ContextService._assert_sleep_quota_or_raise",
        exc=FeatureNotAvailableError(
            "Sleep Maintenance is a PRO-tier feature.",
            feature="sleep_mode",
            required_plan=lowest_tier_with_limit("sleep_enabled_contexts_limit", 0),
            required_plan_display=PLAN_TIERS[
                lowest_tier_with_limit("sleep_enabled_contexts_limit", 0) or _FREE
            ].display_name,
            current_plan=_FREE,
        ),
        site_literal="sleep_mode",
        carries_counts=False,
    ),
    # ---- FEAT-001, gate "allowlist" — the headline fix (S10) -------------
    Refusal(
        id="allowlist/memory_analysis-start",
        site="auth.analysis_gates:require_memory_analysis_access",
        exc=FeatureNotAvailableError.for_rollout(_ANALYSIS_REFUSAL, "memory_analysis"),
        site_literal=_ANALYSIS_REFUSAL,
        carries_counts=False,
    ),
    Refusal(
        id="allowlist/memory_analysis-read",
        site="auth.analysis_gates:require_memory_analysis_read",
        exc=FeatureNotAvailableError.for_rollout(_ANALYSIS_REFUSAL, "memory_analysis"),
        site_literal=_ANALYSIS_REFUSAL,
        carries_counts=False,
    ),
    Refusal(
        id="allowlist/memory_analysis-mcp",
        site="auth.analysis_gates:check_memory_analysis_access_mcp",
        exc=FeatureNotAvailableError.for_rollout(_ANALYSIS_REFUSAL, "memory_analysis"),
        site_literal=_ANALYSIS_REFUSAL,
        carries_counts=False,
    ),
    # ---- VAL-001 with a gate annotation (S13) ----------------------------
    # The managed-LLM refusal keeps its code and its status (§1.3.5 allows
    # exactly two status moves, and this is neither); only the details grow.
    Refusal(
        id="plan/managed_llm",
        site="services.analysis.llm_lane:resolve_analysis_lane",
        exc=ValidationError(
            "BYOK required",
            field="byok",
            **feature_gate_details(_FREE, "managed_llm"),
        ),
        site_literal="byok",
        carries_counts=False,
    ),
    Refusal(
        id="deployment/managed_llm",
        site="services.analysis.llm_lane:resolve_analysis_lane",
        exc=ValidationError(
            "BYOK required",
            field="byok",
            gate=GATE_DEPLOYMENT,
            feature="managed_llm",
        ),
        site_literal="byok",
        carries_counts=False,
    ),
    # ---- QUOTA-001 -------------------------------------------------------
    Refusal(
        id="quota/contexts",
        site="services.quota_service:QuotaService.check_context_creation_allowed",
        exc=QuotaExceededError(
            "Context limit reached.",
            **quota_gate_details(
                _FREE,
                "contexts",
                current=1,
                limit=1,
                required_plan=lowest_tier_with_limit("max_contexts_per_workspace", 1),
            ),
        ),
        site_literal="contexts",
    ),
    Refusal(
        id="quota/members",
        site="services.quota_service:QuotaService.check_member_quota",
        exc=QuotaExceededError(
            "Member limit reached.",
            **quota_gate_details(
                _FREE,
                "members",
                current=1,
                limit=1,
                required_plan=lowest_tier_with_limit("max_members_per_workspace", 1),
                feature="team_invitations",
            ),
        ),
        site_literal="members",
    ),
    Refusal(
        id="quota/memories_per_day",
        site="services.quota_service:QuotaService.check_memories_per_day._refuse",
        exc=QuotaExceededError(
            "Daily memory quota exceeded.",
            **quota_gate_details(
                _FREE,
                "memories_per_day",
                current=100,
                limit=100,
                required_plan=lowest_tier_with_limit("memories_per_day", 100),
                resets_at="2026-01-01T00:00:00+00:00",
            ),
            used_today=100,
            requested=1,
        ),
        site_literal="memories_per_day",
        legacy=frozenset({"used_today", "requested", "limit", "resets_at"}),
    ),
    Refusal(
        id="quota/workspace_limit_reached",
        site="services.quota_service:QuotaService.check_workspace_creation_allowed",
        exc=QuotaExceededError(
            "Workspace limit reached.",
            **quota_gate_details(
                _FREE,
                "workspace_limit_reached",
                current=1,
                limit=1,
                required_plan=PlanName.BASIC,
            ),
            owned_count=1,
            cap=1,
            tier=_FREE,
            next_tier=PlanName.BASIC,
        ),
        site_literal="workspace_limit_reached",
        legacy=frozenset({"owned_count", "cap", "tier", "next_tier"}),
    ),
    Refusal(
        id="quota/memory_analysis",
        site="auth.analysis_gates:check_memory_analysis_quota",
        exc=QuotaExceededError(
            "Analysis daily quota exceeded.",
            **quota_gate_details(
                PlanName.PRO,
                "memory_analysis",
                current=3,
                limit=3,
                required_plan=lowest_tier_with_limit("analysis_runs_per_day", 3),
                feature="memory_analysis",
                resets_at="2026-01-01T00:00:00+00:00",
            ),
            used_today=3,
            limit_today=3,
            addon_bonus=0,
            remaining_today=0,
        ),
        site_literal="memory_analysis",
        legacy=frozenset(
            {"used_today", "limit_today", "addon_bonus", "remaining_today", "resets_at"}
        ),
    ),
    Refusal(
        id="quota/sleep_enabled_contexts",
        site="services.context_service:ContextService._assert_sleep_quota_or_raise",
        exc=QuotaExceededError(
            "Sleep-enabled contexts quota exceeded.",
            **quota_gate_details(
                PlanName.PRO,
                "sleep_enabled_contexts",
                current=3,
                limit=3,
                required_plan=None,
                feature="sleep_mode",
            ),
            addon_bonus=0,
            requested=4,
        ),
        site_literal="sleep_enabled_contexts",
        legacy=frozenset({"addon_bonus", "requested", "limit", "current"}),
    ),
    Refusal(
        id="quota/resource_tokens",
        site="api.routes.resource_tokens:create_resource_token",
        exc=QuotaExceededError(
            "Token limit reached.",
            status_code=403,
            **quota_gate_details(
                _FREE,
                "resource_tokens",
                current=1,
                limit=1,
                required_plan=lowest_tier_with_limit("max_resource_tokens", 1),
                feature="resources",
            ),
        ),
        site_literal="resource_tokens",
    ),
    Refusal(
        id="quota/connectors",
        site=("services.connector_provisioning:ConnectorProvisioningService._raise_seat_cap"),
        exc=_connector_seat_cap(),
        site_literal="connectors",
        legacy=frozenset({"max_connectors", "active_connectors"}),
    ),
    Refusal(
        id="quota/storage_bytes",
        site="services.storage_quota_service:reserve_storage_bytes",
        exc=QuotaExceededError(
            message="Storage quota exceeded.",
            quota_type="storage_bytes",
            limit=100,
            current=100,
            requested=1,
        ),
        site_literal="storage_bytes",
        legacy=frozenset({"limit", "current", "requested"}),
    ),
    Refusal(
        id="quota/agents",
        site="services.agent_registry_service:AgentRegistryService.create_agent",
        exc=QuotaExceededError(
            "Agent registry limit reached.",
            quota_type="agents",
        ),
        site_literal="agents",
        carries_counts=False,
    ),
    # ---- QUOTA-002: a USD cap, so no integer counts exist to ship -------
    Refusal(
        id="quota/embedding_spend_daily",
        site=("services.embedding_spend_cap_service:EmbeddingSpendCapService.check_cap_or_raise"),
        exc=EmbeddingSpendCapExceeded(
            "Daily embedding spend cap reached",
            period="daily",
            cap_usd=0.5,
            current_usd=0.5,
        ),
        site_literal="daily",
        legacy=frozenset({"period", "cap_usd", "current_usd"}),
        carries_counts=False,
    ),
    Refusal(
        id="quota/embedding_spend_monthly",
        site=("services.embedding_spend_cap_service:EmbeddingSpendCapService.check_cap_or_raise"),
        exc=EmbeddingSpendCapExceeded(
            "Monthly embedding spend cap reached",
            period="monthly",
            cap_usd=5.0,
            current_usd=5.0,
        ),
        site_literal="monthly",
        legacy=frozenset({"period", "cap_usd", "current_usd"}),
        carries_counts=False,
    ),
    # ---- The rate-limit family: a rolling per-day counter, no counts ----
    Refusal(
        id="quota/api_public_daily",
        site="api.middleware.rate_limit:RateLimitMiddleware._check_daily_quota",
        exc=_rate_limit_refusal("/api/v1/public/x"),
        site_literal="api_public_daily",
        carries_counts=False,
    ),
    Refusal(
        id="quota/api_rest_daily",
        site="api.middleware.rate_limit:RateLimitMiddleware._check_daily_quota",
        exc=_rate_limit_refusal("/api/v1/contexts"),
        site_literal="api_rest_daily",
        carries_counts=False,
    ),
    Refusal(
        id="quota/api_mcp_daily",
        site="api.middleware.rate_limit:RateLimitMiddleware._check_daily_quota",
        exc=QuotaExceededError(
            "Daily MCP quota exceeded.",
            quota_type="api_mcp_daily",
        ),
        site_literal=None,
        carries_counts=False,
    ),
]

QUOTA_REFUSALS = [r for r in REFUSALS if r.exc.details.get("gate") == GATE_QUOTA]
FEATURE_REFUSALS = [r for r in REFUSALS if r.exc.details.get("gate") != GATE_QUOTA]
PLAN_REFUSALS = [r for r in REFUSALS if r.exc.details.get("gate") == GATE_PLAN]


def _ids(rs: list[Refusal]) -> list[str]:
    return [r.id for r in rs]


# Raise sites that deliberately do NOT participate in the gate contract.
# Each one is a use of a quota/feature exception type for something that is
# not a plan or quota refusal, so annotating it would put a meaningless
# ``gate`` on the wire.
NOT_GATE_REFUSALS: frozenset[str] = frozenset(
    {
        # A 1 MB request-body guard, not a plan cap. Every tier has the same
        # ceiling and no tier raises it, so there is no upgrade to advertise.
        "services.memory_service:MemoryService.remember",
        "services.memory_service:MemoryService._update_guard_size",
        "services.memory_service:MemoryService._patch_guard_size",
        # "Workspace {id} not found" raised as a quota error: a server-side
        # anomaly wearing a quota type. Out of scope for #1644 — giving it a
        # code of its own is a separate change.
        "services.quota_service:QuotaService.check_memories_per_day",
        # Two raises: the same not-found anomaly, and the TOTAL memory cap.
        # The total cap is a genuine quota refusal, but §1.4's frozen
        # vocabulary has no key for it (``memories_per_day`` is the daily
        # rate, not the lifetime ceiling) and the client-side map is frozen
        # against that same set. Annotating it means extending the
        # vocabulary on both sides, which is a follow-up, not this PR.
        "services.quota_service:QuotaService.check_memory_quota",
    }
)


# ---------------------------------------------------------------------------
# Static inventory of the refusal sites in ``backend/src``
# ---------------------------------------------------------------------------

_GATE_EXCEPTIONS = {
    "QuotaExceededError",
    "EmbeddingSpendCapExceeded",
    "FeatureNotAvailableError",
}
_DETAILS_BUILDERS = {"feature_gate_details", "quota_gate_details"}


def _string_constants(node: ast.AST) -> set[str]:
    return {
        c.value for c in ast.walk(node) if isinstance(c, ast.Constant) and isinstance(c.value, str)
    }


def _is_gate_refusal(call: ast.Call) -> bool:
    """True when this ``raise X(...)`` participates in the gate contract.

    Three shapes qualify: one of the gate exception types (directly or via a
    ``for_*`` constructor); any raise carrying an explicit ``gate=``; and any
    raise splatting one of the details builders — which is how the
    ``CONNECTOR-001`` seat cap and the managed-LLM ``VAL-001`` join in
    without being instances of the gate types.
    """
    func = call.func
    if isinstance(func, ast.Name):
        cls: str | None = func.id
    elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        cls = func.value.id
    else:
        cls = None
    if cls in _GATE_EXCEPTIONS:
        return True
    if any(kw.arg == "gate" for kw in call.keywords):
        return True
    for kw in call.keywords:
        if kw.arg is not None:
            continue
        # ``**builder(...)`` — possibly behind a conditional expression.
        for sub in ast.walk(kw.value):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id in _DETAILS_BUILDERS
            ):
                return True
    return False


class _RefusalVisitor(ast.NodeVisitor):
    """Collect every gate-refusal ``raise`` with the qualname enclosing it."""

    def __init__(self) -> None:
        self._stack: list[str] = []
        self.raises: list[tuple[str, ast.Call]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Raise(self, node: ast.Raise) -> None:
        if isinstance(node.exc, ast.Call) and _is_gate_refusal(node.exc):
            self.raises.append((".".join(self._stack), node.exc))
        self.generic_visit(node)


def _refusal_raises(path: pathlib.Path) -> list[tuple[str, ast.Call]]:
    visitor = _RefusalVisitor()
    visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
    return visitor.raises


def _discover_refusal_sites() -> dict[str, set[str]]:
    """Map ``module:qualname`` -> the string constants of its refusal raises."""
    found: dict[str, set[str]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        module = str(path.relative_to(SRC_ROOT))[: -len(".py")].replace("/", ".")
        for qualname, call in _refusal_raises(path):
            found.setdefault(f"{module}:{qualname}", set()).update(_string_constants(call))
    return found


DISCOVERED_SITES = _discover_refusal_sites()


class TestTheEnumerationIsComplete:
    """Layers 2 and 3: the table describes the code that exists, and all of it."""

    def test_the_enumeration_covers_every_refusal_site(self) -> None:
        """A refusal that nobody classified is a refusal with no contract.

        Adding a raise to ``backend/src`` fails here until it is either
        listed in ``REFUSALS`` with its vocabulary or justified in
        ``NOT_GATE_REFUSALS``.
        """
        enumerated = {r.site for r in REFUSALS}
        unclassified = set(DISCOVERED_SITES) - enumerated - NOT_GATE_REFUSALS
        assert not unclassified, (
            "these refusal sites carry no declared gate vocabulary — add them to "
            f"REFUSALS or justify them in NOT_GATE_REFUSALS: {sorted(unclassified)}"
        )

    def test_the_enumeration_has_not_gone_stale(self) -> None:
        """Every site in the table still raises a refusal."""
        enumerated = {r.site for r in REFUSALS}
        vanished = enumerated - set(DISCOVERED_SITES)
        assert not vanished, (
            f"REFUSALS names sites that no longer raise a gate refusal: {sorted(vanished)}"
        )

    @pytest.mark.parametrize(
        "refusal",
        [r for r in REFUSALS if r.site_literal is not None],
        ids=_ids([r for r in REFUSALS if r.site_literal is not None]),
    )
    def test_each_enumerated_refusal_still_matches_its_site(self, refusal: Refusal) -> None:
        """The discriminator in the table is still the one in the code.

        A renamed ``quota_type`` or ``feature`` is a silent wire break: the
        server keeps answering and the client keeps parsing, but the branch
        that used to fire never fires again.
        """
        assert refusal.site_literal in DISCOVERED_SITES[refusal.site], (
            f"{refusal.id}: {refusal.site_literal!r} is no longer raised at "
            f"{refusal.site} — the table and the code have drifted apart"
        )


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------


class TestGateVocabulary:
    @pytest.mark.parametrize("refusal", REFUSALS, ids=_ids(REFUSALS))
    def test_every_gate_refusal_declares_a_known_gate_kind(self, refusal: Refusal) -> None:
        gate = refusal.exc.details.get("gate")
        assert gate in GATE_KINDS, f"{refusal.id}: unknown gate {gate!r}"

    @pytest.mark.parametrize("refusal", REFUSALS, ids=_ids(REFUSALS))
    def test_no_refusal_serializes_the_role_gate(self, refusal: Refusal) -> None:
        """``GATE_ROLE`` is a vocabulary member, never a wire value (§1.3.4).

        Role refusals are ``AUTH-101`` and have their details stripped as
        CWE-639 defence in depth; a client identifies them by the code.
        """
        assert refusal.exc.details.get("gate") != GATE_ROLE

    @pytest.mark.parametrize("refusal", QUOTA_REFUSALS, ids=_ids(QUOTA_REFUSALS))
    def test_every_quota_refusal_declares_a_known_quota_type(self, refusal: Refusal) -> None:
        quota_type = refusal.exc.details.get("quota_type")
        assert quota_type in QUOTA_TYPES, f"{refusal.id}: unknown quota_type {quota_type!r}"

    def test_quota_types_are_exactly_the_frozen_vocabulary(self) -> None:
        """Both directions: no unused vocabulary, no unlisted refusal.

        The client's ``QUOTA_TYPE_TO_GATE_KEY`` asserts its domain is exactly
        this set, so an entry with no live refusal is an unreachable client
        branch and a refusal with no entry is an unrendered gate.
        """
        emitted = {r.exc.details["quota_type"] for r in QUOTA_REFUSALS}
        assert emitted == set(QUOTA_TYPES), (
            f"only emitted by the vocabulary: {sorted(set(QUOTA_TYPES) - emitted)}; "
            f"emitted but not frozen: {sorted(emitted - set(QUOTA_TYPES))}"
        )

    @pytest.mark.parametrize("refusal", FEATURE_REFUSALS, ids=_ids(FEATURE_REFUSALS))
    def test_every_feature_refusal_names_its_feature(self, refusal: Refusal) -> None:
        """``feature`` is REQUIRED on a non-quota gate (§1.3.1)."""
        assert refusal.exc.details.get("feature"), f"{refusal.id}: no feature key"


# ---------------------------------------------------------------------------
# The fields
# ---------------------------------------------------------------------------


class TestGateDetailFields:
    @pytest.mark.parametrize("refusal", PLAN_REFUSALS, ids=_ids(PLAN_REFUSALS))
    def test_feat001_details_carry_required_plan_key_and_display(self, refusal: Refusal) -> None:
        """Both halves ship (§1.3.3): the key a UI decides with, the label a
        CLI renders. They must agree, and the display must be the tier's own
        ``display_name`` so a ``PLAN_*_DISPLAY_NAME`` override flows through.
        """
        details = refusal.exc.details
        required = details.get("required_plan")
        if required is None:
            assert details.get("required_plan_display") is None
            return
        assert required in PLAN_TIERS, f"{refusal.id}: {required!r} is not a tier key"
        assert details["required_plan_display"] == PLAN_TIERS[required].display_name

    @pytest.mark.parametrize(
        "refusal",
        [r for r in REFUSALS if r.exc.details.get("gate") in (GATE_ALLOWLIST, GATE_DEPLOYMENT)],
        ids=_ids(
            [r for r in REFUSALS if r.exc.details.get("gate") in (GATE_ALLOWLIST, GATE_DEPLOYMENT)]
        ),
    )
    def test_allowlist_and_deployment_refusals_are_plan_neutral(self, refusal: Refusal) -> None:
        """A2/A4: no tier the caller can buy lifts these, so nothing in the
        block may be read as an upsell."""
        details = refusal.exc.details
        assert details.get("required_plan") is None
        assert details.get("required_plan_display") is None

    def test_feat001_required_plan_is_none_when_no_tier_carries_the_feature(self) -> None:
        """A ``PLAN_<KEY>_FEATURES`` override can strip a feature from every
        tier (#1559). The refusal must still answer 403 and must advertise no
        upgrade rather than 500 or invent one."""
        from config import plan_tiers

        stripped = {
            k: v for k, v in plan_tiers.FEATURE_MIN_PLANS.items() if k != "team_invitations"
        }
        with patch.dict(plan_tiers.FEATURE_MIN_PLANS, stripped, clear=True):
            exc = FeatureNotAvailableError.for_feature(_FREE, "team_invitations")

        assert exc.status_code == 403
        assert exc.error_code == "FEAT-001"
        assert exc.details["gate"] == GATE_PLAN
        assert exc.details["feature"] == "team_invitations"
        assert exc.details["required_plan"] is None
        assert exc.details["required_plan_display"] is None

    def test_required_plan_display_is_null_never_the_higher_fallback(self) -> None:
        """``required_plan_display_name`` answers the prose fallback
        ``"higher"`` for an unknown feature, which reads fine mid-sentence
        and is useless as a label. The details block emits ``None``."""
        from config import plan_tiers

        stripped = {
            k: v for k, v in plan_tiers.FEATURE_MIN_PLANS.items() if k != "team_invitations"
        }
        with patch.dict(plan_tiers.FEATURE_MIN_PLANS, stripped, clear=True):
            assert required_plan_display_name("team_invitations") == "higher"
            details = feature_gate_details(_FREE, "team_invitations")

        assert details["required_plan_display"] is None
        assert all(v != "higher" for v in details.values())

    @pytest.mark.parametrize(
        "refusal",
        [r for r in QUOTA_REFUSALS if r.carries_counts],
        ids=_ids([r for r in QUOTA_REFUSALS if r.carries_counts]),
    )
    def test_quota001_details_carry_current_and_limit_as_ints(self, refusal: Refusal) -> None:
        details = refusal.exc.details
        for key in ("current", "limit"):
            assert key in details, f"{refusal.id}: no {key}"
            assert isinstance(details[key], int) and not isinstance(details[key], bool), (
                f"{refusal.id}: {key} is {type(details[key]).__name__}, not int — a client "
                "renders it into 'N of M used' and cannot parse prose or a float"
            )

    @pytest.mark.parametrize(
        "refusal",
        [r for r in QUOTA_REFUSALS if not r.carries_counts],
        ids=_ids([r for r in QUOTA_REFUSALS if not r.carries_counts]),
    )
    def test_the_countless_quota_families_ship_no_half_counts(self, refusal: Refusal) -> None:
        """§1.3.2: the USD spend caps and the rate-limit family genuinely have
        no integer counts. The client renders ``descriptionNoNumbers`` for
        them, which it selects on the ABSENCE of the keys — so shipping one
        of the two would make it interpolate a number it does not have."""
        details = refusal.exc.details
        assert "current" not in details and "limit" not in details, (
            f"{refusal.id}: ships a partial count pair {details!r}"
        )


class TestBackCompat:
    """Nothing pre-#1644 may be renamed or dropped — the canonical names are
    ADDED beside the legacy ones, because an older client is still reading
    the old names off the same body."""

    @pytest.mark.parametrize(
        "refusal",
        [r for r in REFUSALS if r.legacy],
        ids=_ids([r for r in REFUSALS if r.legacy]),
    )
    def test_legacy_detail_fields_are_still_present(self, refusal: Refusal) -> None:
        missing = refusal.legacy - set(refusal.exc.details)
        assert not missing, (
            f"{refusal.id}: legacy detail field(s) {sorted(missing)} disappeared — an "
            "older client reading them off this body breaks silently"
        )

    @pytest.mark.parametrize(
        "refusal",
        [r for r in REFUSALS if r.legacy],
        ids=_ids([r for r in REFUSALS if r.legacy]),
    )
    def test_the_legacy_fields_are_still_passed_at_the_site(self, refusal: Refusal) -> None:
        """Read off the raise itself, not off a reconstruction of it.

        ``REFUSALS`` builds each exception through the same helpers the site
        uses, so an assertion on ``exc.details`` alone would still pass if
        the SITE dropped ``used_today``. This re-reads the source and checks
        the keyword is really still there.
        """
        module, _, qualname = refusal.site.partition(":")
        path = SRC_ROOT / (module.replace(".", "/") + ".py")
        passed: set[str] = set()
        for found_qualname, call in _refusal_raises(path):
            if found_qualname != qualname:
                continue
            for kw in call.keywords:
                if kw.arg is not None:
                    passed.add(kw.arg)
                else:
                    # ``**quota_gate_details(current=..., resets_at=...)``
                    for sub in ast.walk(kw.value):
                        if isinstance(sub, ast.Call):
                            passed.update(k.arg for k in sub.keywords if k.arg is not None)

        missing = refusal.legacy - passed
        assert not missing, (
            f"{refusal.id}: {sorted(missing)} no longer passed at {refusal.site} — "
            "the canonical names are ADDED beside the legacy ones, never instead of them"
        )


class TestRoleRefusalsCarryNothing:
    @pytest.mark.asyncio
    async def test_gate_details_never_leak_a_reason_for_authorization_error(self) -> None:
        """§1.3.4: ``AuthorizationError`` details are stripped wholesale, and
        #1644 does not touch that. A ``gate`` annotation must not become the
        loophole that puts workspace forensics back on the wire (CWE-639)."""
        from api.main import memory_cloud_exception_handler

        request = MagicMock()
        request.url.path = "/api/v1/workspaces/x/members"

        denied = AuthorizationError("Insufficient permissions")
        denied.details["gate"] = GATE_ROLE
        denied.details["reason"] = "user is not a member of workspace 0000"

        response = await memory_cloud_exception_handler(request, denied)
        body = response.body.decode()

        assert response.status_code == 403
        assert '"details":{}' in body.replace(" ", "")
        assert GATE_ROLE not in body
        assert "not a member" not in body

    @pytest.mark.asyncio
    async def test_a_gate_refusal_still_reaches_the_client_intact(self) -> None:
        """The other half of the same handler: the strip is type-scoped, so a
        plan refusal keeps its whole details block."""
        from api.main import memory_cloud_exception_handler

        request = MagicMock()
        request.url.path = "/api/v1/invitations"

        response = await memory_cloud_exception_handler(
            request, FeatureNotAvailableError.for_feature(_FREE, "team_invitations")
        )
        body = response.body.decode()

        assert response.status_code == 403
        assert '"gate":"plan"' in body.replace(" ", "")
        assert "team_invitations" in body


# ---------------------------------------------------------------------------
# Limits that are NOT gates carry no gate
# ---------------------------------------------------------------------------

_OVERSIZED = "x" * (MAX_CONTENT_SIZE + 1)


async def _rest_details(exc: MemoryCloudException) -> dict:
    """The ``details`` block of the REST body the global handler renders."""
    from api.main import memory_cloud_exception_handler

    request = MagicMock()
    request.url.path = "/api/v1/memory"
    response = await memory_cloud_exception_handler(request, exc)
    body = json.loads(response.body)
    assert body["error"] == exc.error_code
    return body["details"]


def _stored_memory() -> SimpleNamespace:
    return SimpleNamespace(summary="stored summary", context_summary="", content="", details=None)


def _execute_returning(*results: object) -> MagicMock:
    """A mock session whose ``execute`` answers ``results`` in order."""
    db = MagicMock()
    db.execute = AsyncMock(side_effect=list(results))
    return db


def _workspace_result(workspace: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = workspace
    return result


async def _raised_by(awaitable) -> QuotaExceededError:
    with pytest.raises(QuotaExceededError) as exc:
        await awaitable
    return exc.value


async def _remember_size_guard() -> QuotaExceededError:
    from services.memory_service import MemoryService

    service = MemoryService(_execute_returning())
    context = MagicMock()
    context.id = uuid4()
    context.workspace_id = uuid4()
    service._get_context_isolation_params = AsyncMock(  # type: ignore[method-assign]
        return_value=(context, str(context.workspace_id), str(context.id))
    )
    permissive = MagicMock(
        check_memory_quota=AsyncMock(return_value=(True, None)),
        check_memories_per_day=AsyncMock(return_value=(True, None)),
    )
    with patch("services.quota_service.QuotaService", return_value=permissive):
        return await _raised_by(
            service.remember(
                RememberRequest(summary="a summary long enough", content=_OVERSIZED, type="note"),
                user_id="u",
                current_context_id=context.id,
            )
        )


async def _update_size_guard() -> QuotaExceededError:
    from services.memory_service import MemoryService

    with pytest.raises(QuotaExceededError) as exc:
        MemoryService._update_guard_size(
            _stored_memory(), UpdateMemoryRequest(memory_id=uuid4(), content=_OVERSIZED), None, None
        )
    return exc.value


async def _patch_size_guard() -> QuotaExceededError:
    from services.memory_service import MemoryService

    with pytest.raises(QuotaExceededError) as exc:
        MemoryService._patch_guard_size(
            _stored_memory(), PatchMemoryRequest(content=_OVERSIZED), {"content"}, None
        )
    return exc.value


async def _memories_per_day_workspace_missing() -> QuotaExceededError:
    from services.quota_service import QuotaService

    db = _execute_returning(_workspace_result(None))
    return await _raised_by(
        QuotaService(db).check_memories_per_day(uuid4(), raise_on_exceeded=True)
    )


async def _memory_quota_workspace_missing() -> QuotaExceededError:
    from services.quota_service import QuotaService

    db = _execute_returning(_workspace_result(None))
    return await _raised_by(QuotaService(db).check_memory_quota(uuid4(), raise_on_exceeded=True))


async def _memory_quota_total_cap() -> QuotaExceededError:
    from services.quota_service import QuotaService

    count = MagicMock()
    count.scalar.return_value = 1000
    db = _execute_returning(_workspace_result(SimpleNamespace(plan_name=_FREE)), count)
    effective = MagicMock(get_effective_quotas=AsyncMock(return_value={"memory_limit": 1000}))
    with patch("services.quota_service.EffectiveQuotaService", return_value=effective):
        return await _raised_by(
            QuotaService(db).check_memory_quota(uuid4(), raise_on_exceeded=True)
        )


# Every raise at every ``NOT_GATE_REFUSALS`` site, driven through the real
# code. Keyed by site so a new entry in ``NOT_GATE_REFUSALS`` fails
# ``test_every_non_gate_site_is_driven`` until it is exercised here too.
NON_GATE_DRIVERS = {
    "services.memory_service:MemoryService.remember": [_remember_size_guard],
    "services.memory_service:MemoryService._update_guard_size": [_update_size_guard],
    "services.memory_service:MemoryService._patch_guard_size": [_patch_size_guard],
    "services.quota_service:QuotaService.check_memories_per_day": [
        _memories_per_day_workspace_missing
    ],
    "services.quota_service:QuotaService.check_memory_quota": [
        _memory_quota_workspace_missing,
        _memory_quota_total_cap,
    ],
}
_NON_GATE_CASES = [
    pytest.param(driver, id=f"{site.rpartition('.')[2]}:{driver.__name__}")
    for site, drivers in NON_GATE_DRIVERS.items()
    for driver in drivers
]


class TestLimitsThatAreNotGatesCarryNoGate:
    """``QuotaExceededError`` is also raised for things no tier lifts.

    A ``gate`` on those would make a client offer an upgrade for a 1 MB
    request-size limit. The constructor stamps ``gate`` only for a TYPED
    refusal (a ``quota_type`` from ``QUOTA_TYPES``), so every untyped raise
    reaches the wire with no gate — asserted here on the REST body the real
    sites produce, not on a reconstruction of it.
    """

    def test_every_non_gate_site_is_driven(self) -> None:
        assert set(NON_GATE_DRIVERS) == set(NOT_GATE_REFUSALS)

    @pytest.mark.parametrize(
        "site",
        sorted(NOT_GATE_REFUSALS),
    )
    def test_the_non_gate_sites_raise_untyped(self, site: str) -> None:
        """Statically: no raise at these sites passes a ``quota_type``, a
        ``gate`` or a details builder, so nothing can type it by accident."""
        module, _, qualname = site.partition(":")
        path = SRC_ROOT / (module.replace(".", "/") + ".py")
        calls = [call for found, call in _refusal_raises(path) if found == qualname]
        assert calls, f"{site} raises nothing"
        for call in calls:
            assert len(call.args) <= 1, f"{site}: a positional quota_type is passed"
            assert all(kw.arg not in ("quota_type", "gate") for kw in call.keywords), site
            assert all(kw.arg is not None for kw in call.keywords), (
                f"{site}: a ``**`` splat could carry quota_type / gate"
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("driver", _NON_GATE_CASES)
    async def test_the_rest_body_carries_no_gate(self, driver) -> None:
        exc = await driver()
        details = await _rest_details(exc)

        assert exc.error_code == "QUOTA-001"
        assert "gate" not in details
        # The whole block, pinned: ``frontend/src/lib/gates/featureGates.test.ts``
        # feeds this exact body to ``normalizeGate`` and asserts it yields no
        # gate — the QUOTA-001 code fallback needs a frozen ``quota_type``.
        assert details == {"quota_type": None}


# ---------------------------------------------------------------------------
# The MCP envelope carries the same gate as the REST body
# ---------------------------------------------------------------------------

# The keys a client decides with. Every one the REST body carries must reach
# the MCP envelope with the same value; the quota mappers drop ``None``
# values by long-standing convention, which reads identically to a client.
GATE_DETAIL_KEYS = (
    "gate",
    "feature",
    "quota_type",
    "current",
    "limit",
    "required_plan",
    "required_plan_display",
    "current_plan",
    "resets_at",
)


def _mcp_payload(result) -> dict:
    assert len(result) == 1
    return json.loads(result[0].text)


async def _mcp_analysis(exc: MemoryCloudException) -> dict:
    from mcp_server.tools.analysis import _gate_error_response

    return _mcp_payload(_gate_error_response(exc, "analyze_context"))


async def _mcp_files(exc: MemoryCloudException) -> dict:
    from mcp_server.tools.files import _exc_to_error_response

    return _mcp_payload(_exc_to_error_response(exc))


def _db_yielding() -> tuple[object, MagicMock]:
    db = MagicMock()
    db.rollback = AsyncMock()
    db.commit = AsyncMock()

    async def get_db():
        yield db

    return get_db, db


async def _mcp_create_context(exc: MemoryCloudException) -> dict:
    from mcp_server.tools.context import handle_create_context

    get_db, _ = _db_yielding()
    service = MagicMock(create_context=AsyncMock(side_effect=exc))
    with (
        patch("db.base.get_db", new=get_db),
        patch(
            "mcp_server.tools.context._get_workspace_member_role",
            new=AsyncMock(return_value="owner"),
        ),
        patch(
            "services.quota_service.QuotaService.check_context_creation_allowed",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch("services.context_service.ContextService", return_value=service),
        patch("mcp_server.tools.context._log_tool_usage", new_callable=AsyncMock),
    ):
        return _mcp_payload(
            await handle_create_context(
                args={"name": "team-ctx", "is_private": False},
                user_id="u",
                workspace_id=uuid4(),
            )
        )


async def _mcp_setup_connector(exc: MemoryCloudException) -> dict:
    from mcp_server.tools.resource import handle_setup_connector

    get_db, _ = _db_yielding()
    service = MagicMock(provision_connector=AsyncMock(side_effect=exc))
    with (
        patch("db.base.get_db", new=get_db),
        patch(
            "mcp_server.tools.resource._check_owner_admin_role",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "services.connector_provisioning.ConnectorProvisioningService",
            return_value=service,
        ),
        patch("mcp_server.tools.resource._log_tool_usage", new_callable=AsyncMock),
    ):
        return _mcp_payload(
            await handle_setup_connector(
                {"connector_type": "slack", "resource_id": "slack-team"},
                "u",
                uuid4(),
            )
        )


async def _mcp_remember(exc: MemoryCloudException) -> dict:
    from mcp_server.tools.memory import handle_remember

    get_db, _ = _db_yielding()
    context = MagicMock()
    context.id = uuid4()
    service = MagicMock(remember=AsyncMock(side_effect=exc))
    with (
        patch("db.base.get_db", new=get_db),
        patch(
            "mcp_server.tools.memory._check_viewer_permission",
            new=AsyncMock(return_value=None),
        ),
        patch("mcp_server.tools.memory._resolve_context", new=AsyncMock(return_value=context)),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
    ):
        return _mcp_payload(
            await handle_remember(
                {
                    "context_id": str(uuid4()),
                    "summary": "a summary long enough",
                    "content": "c",
                    "type": "note",
                },
                "u",
                uuid4(),
            )
        )


async def _mcp_register_agent(exc: MemoryCloudException) -> dict:
    from mcp_server.tools.agent_registry import handle_register_agent

    get_db, _ = _db_yielding()
    service = MagicMock(create_agent=AsyncMock(side_effect=exc))
    with (
        patch("db.base.get_db", new=get_db),
        patch(
            "mcp_server.tools.resource._check_owner_admin_role",
            new=AsyncMock(return_value=None),
        ),
        patch("services.agent_registry_service.AgentRegistryService", return_value=service),
    ):
        return _mcp_payload(await handle_register_agent({"name": "ci-bot"}, "u", uuid4()))


def _context_cap(exc: MemoryCloudException):
    """``check_context_creation_allowed`` refusing the way the real one does.

    It raises ``exc`` only in its raising form and answers ``(False, prose)``
    otherwise — so a handler that calls the non-raising form gets the prose
    and no details, exactly as in production.
    """

    async def check(*_args: object, raise_on_denied: bool = False, **_kwargs: object):
        if raise_on_denied:
            raise exc
        return False, exc.message

    return check


async def _mcp_create_context_cap(exc: MemoryCloudException) -> dict:
    """``create_context`` refused by the context cap it checks itself."""
    from mcp_server.tools.context import handle_create_context

    get_db, _ = _db_yielding()
    with (
        patch("db.base.get_db", new=get_db),
        patch(
            "mcp_server.tools.context._get_workspace_member_role",
            new=AsyncMock(return_value="owner"),
        ),
        patch(
            "services.quota_service.QuotaService.check_context_creation_allowed",
            new=_context_cap(exc),
        ),
        patch("mcp_server.tools.context._log_tool_usage", new_callable=AsyncMock),
    ):
        return _mcp_payload(
            await handle_create_context(
                args={"name": "team-ctx"},
                user_id="u",
                workspace_id=uuid4(),
            )
        )


def _plan_with(feature: str) -> str:
    from config.plan_tiers import has_feature

    return next(name for name in PLAN_TIERS if has_feature(name, feature))


async def _setup_resource_preflight_payload(plan_name: str, cap: object) -> dict:
    """Drive ``setup_resource``'s gates up to the plan and context-cap checks."""
    from mcp_server.tools.resource import _setup_resource_preflight

    db = _execute_returning(_workspace_result(None), _workspace_result(plan_name))
    with (
        patch(
            "mcp_server.tools.resource._check_owner_admin_role",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "services.context_service.ContextService.get_context_by_name_for_workspace",
            new=AsyncMock(return_value=None),
        ),
        patch("services.quota_service.QuotaService.check_context_creation_allowed", new=cap),
    ):
        error, _ = await _setup_resource_preflight(db, "u", uuid4(), "res-ctx", "res-1")
    assert error is not None, "the preflight did not refuse"
    return _mcp_payload(error)


async def _mcp_setup_resource_plan(exc: MemoryCloudException) -> dict:
    """``setup_resource`` refused by the ``resources`` feature gate."""
    return await _setup_resource_preflight_payload(
        exc.details["current_plan"], AsyncMock(return_value=(True, None))
    )


async def _mcp_setup_resource_context_cap(exc: MemoryCloudException) -> dict:
    """``setup_resource`` refused by the context cap, on a tier with resources."""
    return await _setup_resource_preflight_payload(_plan_with("resources"), _context_cap(exc))


async def _mcp_setup_resource_token_cap(exc: MemoryCloudException) -> dict:
    """``setup_resource``'s token cap, from the facts the handler holds.

    The envelope is built (not raised) from the plan and the active count, so
    this feeds the helper the same facts the REST refusal was built from and
    checks it derives the same block — upgrade tier included.
    """
    from mcp_server.tools.resource import _token_cap_error_response

    details = exc.details
    plan = SimpleNamespace(max_resource_tokens=details["limit"])
    return _mcp_payload(
        _token_cap_error_response(details["current_plan"], plan, details["current"])
    )


async def _mcp_public_flag(exc: MemoryCloudException) -> dict:
    """``update_context(is_public=True)`` refused by the ``public_contexts`` gate."""
    from mcp_server.tools.context import _apply_public_flag

    db = MagicMock()
    db.get = AsyncMock(return_value=SimpleNamespace(plan_name=exc.details["current_plan"]))
    context = SimpleNamespace(is_public=False, workspace_id=uuid4(), resource_id=None)
    error = await _apply_public_flag(db, context, True)
    assert error is not None, "the public flag was not refused"
    return _mcp_payload(error)


@dataclass(frozen=True)
class McpRoute:
    """One MCP door to a refusal.

    Attributes:
        driver: Produces the MCP payload for the refusal's exception through
            the real handler or mapper.
        error: The envelope's ``error`` code. Pinned: an MCP client branches
            on it exactly as a REST client branches on ``VAL-001``.
        legacy: Keys this envelope carried before #1644 (read off
            ``origin/main``). The gate block is ADDED beside them, never
            instead of them — the MCP half of ``TestBackCompat``.
    """

    driver: object
    error: str
    legacy: frozenset[str] = frozenset()


_FEAT = "feature_not_available"
_QUOTA = "quota_exceeded"
_PLAN_REQUIRED = "plan_required"

# Each refusal that reaches an MCP tool, and every door it reaches it through.
MCP_ROUTES: dict[str, tuple[McpRoute, ...]] = {
    "plan/memory_analysis-mcp": (McpRoute(_mcp_analysis, _FEAT, frozenset({"feature"})),),
    "allowlist/memory_analysis-mcp": (McpRoute(_mcp_analysis, _FEAT, frozenset({"feature"})),),
    "quota/memory_analysis": (
        McpRoute(
            _mcp_analysis,
            _QUOTA,
            frozenset(
                {
                    "quota_type",
                    "used_today",
                    "limit_today",
                    "addon_bonus",
                    "remaining_today",
                    "resets_at",
                }
            ),
        ),
    ),
    "plan/managed_llm": (McpRoute(_mcp_analysis, "validation_error", frozenset({"field"})),),
    "deployment/managed_llm": (McpRoute(_mcp_analysis, "validation_error", frozenset({"field"})),),
    # Was ``validation_error`` before #1644 S11 moved the refusal to FEAT-001
    # (decisions §6 item 10) — the one deliberate MCP code change.
    "plan/shared_contexts-service": (McpRoute(_mcp_create_context, _PLAN_REQUIRED),),
    "plan/public_contexts": (
        McpRoute(_mcp_public_flag, _PLAN_REQUIRED, frozenset({"required_plan"})),
    ),
    "plan/resources": (
        McpRoute(_mcp_setup_resource_plan, _PLAN_REQUIRED, frozenset({"required_plan"})),
    ),
    "plan/connectors": (
        McpRoute(_mcp_setup_connector, _PLAN_REQUIRED, frozenset({"required_plan", "feature"})),
    ),
    "quota/connectors": (
        McpRoute(
            _mcp_setup_connector,
            "CONNECTOR-001",
            frozenset({"max_connectors", "active_connectors"}),
        ),
    ),
    "quota/contexts": (
        McpRoute(_mcp_create_context_cap, _QUOTA, frozenset({"help"})),
        McpRoute(_mcp_setup_resource_context_cap, _QUOTA, frozenset({"help"})),
    ),
    "quota/resource_tokens": (
        McpRoute(_mcp_setup_resource_token_cap, _QUOTA, frozenset({"help"})),
    ),
    "quota/memories_per_day": (
        McpRoute(
            _mcp_remember,
            _QUOTA,
            frozenset({"quota_type", "limit", "used_today", "requested", "resets_at"}),
        ),
    ),
    "quota/storage_bytes": (McpRoute(_mcp_files, _QUOTA),),
    "quota/agents": (McpRoute(_mcp_register_agent, _QUOTA),),
}

# Every other refusal, and why no MCP tool sees it. A new refusal must land
# in exactly one of the two tables (``test_every_refusal_is_routed_or_excused``).
NOT_ON_MCP = {
    "plan/team_invitations": "REST route only; no MCP tool invites members",
    "plan/shared_contexts-rest": "the REST route's own pre-check",
    "plan/public_contexts-api-key": "REST route only",
    "plan/any-feature-via-quota-service": "no caller uses the raising form",
    "plan/memory_analysis": "REST dependency; MCP goes through the _mcp variant",
    "allowlist/memory_analysis-start": "REST dependency; MCP goes through the _mcp variant",
    "allowlist/memory_analysis-read": "REST dependency; MCP goes through the _mcp variant",
    "plan/sleep_mode": "REST only; MCP update_context has no sleep_mode field",
    "quota/sleep_enabled_contexts": "REST only; MCP update_context has no sleep_mode field",
    "quota/members": "REST only (invitation create and accept)",
    "quota/workspace_limit_reached": "REST route only; no MCP tool creates workspaces",
    "plan/managed_embeddings": (
        "no MCP handler maps it; it reaches the dispatcher catch-all, whose "
        "{error: str(e)} shape is frozen"
    ),
    "quota/embedding_spend_daily": "no MCP handler maps it (dispatcher catch-all)",
    "quota/embedding_spend_monthly": "no MCP handler maps it (dispatcher catch-all)",
    "quota/api_public_daily": "HTTP middleware; not an MCP path",
    "quota/api_rest_daily": "HTTP middleware; not an MCP path",
    "quota/api_mcp_daily": (
        "HTTP middleware: the /mcp request is refused with the REST body itself"
    ),
}

_MCP_CASES = [
    pytest.param(refusal, route, id=f"{refusal.id}-{route.driver.__name__}")
    for refusal in REFUSALS
    for route in MCP_ROUTES.get(refusal.id, ())
]


class TestTheMcpEnvelopeCarriesTheSameGate:
    """REST and MCP are two doors to the same refusal.

    A plan refusal and a rollout refusal used to be wire-identical over MCP
    after they had been told apart on REST: the analysis mapper forwarded
    ``feature`` and nothing else. Every gate key the REST body carries must
    reach the MCP envelope unchanged.
    """

    def test_every_refusal_is_routed_or_excused(self) -> None:
        ids = {r.id for r in REFUSALS}
        assert set(MCP_ROUTES).isdisjoint(NOT_ON_MCP)
        assert set(MCP_ROUTES) | set(NOT_ON_MCP) == ids, (
            f"unrouted: {sorted(ids - set(MCP_ROUTES) - set(NOT_ON_MCP))}; "
            f"stale: {sorted((set(MCP_ROUTES) | set(NOT_ON_MCP)) - ids)}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("refusal", "route"), _MCP_CASES)
    async def test_mcp_envelope_carries_the_rest_gate_details(
        self, refusal: Refusal, route: McpRoute
    ) -> None:
        rest = await _rest_details(refusal.exc)
        mcp = await route.driver(refusal.exc)

        assert mcp["status"] == "error"
        assert mcp.get("gate") == rest["gate"]
        for key in GATE_DETAIL_KEYS:
            if key not in rest:
                continue
            if rest[key] is None:
                assert mcp.get(key) is None, f"{refusal.id}: {key} is {mcp.get(key)!r} on MCP"
            else:
                assert mcp.get(key) == rest[key], (
                    f"{refusal.id}: {key} is {rest[key]!r} on REST but {mcp.get(key)!r} on MCP"
                )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("refusal", "route"), _MCP_CASES)
    async def test_mcp_envelope_keeps_its_code_and_pre_1644_keys(
        self, refusal: Refusal, route: McpRoute
    ) -> None:
        """The MCP half of ``TestBackCompat``: the gate block is added, and
        nothing an MCP client read before — the ``error`` code, ``message``,
        the per-envelope keys — is renamed or dropped."""
        mcp = await route.driver(refusal.exc)

        assert mcp["error"] == route.error
        assert isinstance(mcp.get("message"), str) and mcp["message"]
        missing = route.legacy - set(mcp)
        assert not missing, f"{refusal.id}: MCP envelope lost {sorted(missing)}"

    @pytest.mark.asyncio
    async def test_plan_and_rollout_refusals_differ_on_mcp(self) -> None:
        """The headline: the two analysis refusals no longer read the same."""
        by_id = {r.id: r for r in REFUSALS}
        plan = await _mcp_analysis(by_id["plan/memory_analysis-mcp"].exc)
        rollout = await _mcp_analysis(by_id["allowlist/memory_analysis-mcp"].exc)

        assert (plan["gate"], rollout["gate"]) == (GATE_PLAN, GATE_ALLOWLIST)
        assert plan["required_plan"] is not None
        assert rollout.get("required_plan") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("driver", _NON_GATE_CASES)
    @pytest.mark.parametrize("door", [_mcp_remember, _mcp_create_context_cap, _mcp_files])
    async def test_mcp_adds_no_gate_to_a_limit_that_is_not_one(self, driver, door) -> None:
        """The MCP quota envelopes forward ``exc.details``; they must not
        invent a ``gate`` the REST body does not carry. ``remember`` is the
        tool the size guard and the memory caps actually reach; the other two
        doors pin the same forwarding in the other mappers."""
        mcp = await door(await driver())

        assert mcp["error"] == "quota_exceeded"
        assert "gate" not in mcp
        assert "quota_type" not in mcp
