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
import pathlib
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from api.middleware.rate_limit import RateLimitMiddleware
from config.constants import (
    GATE_ALLOWLIST,
    GATE_DEPLOYMENT,
    GATE_KINDS,
    GATE_PLAN,
    GATE_QUOTA,
    GATE_ROLE,
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
