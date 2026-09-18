"""Stage [A.5]: which LLM lane a Memory Analysis run uses (#1569).

Before #1569 the analysis pipeline had exactly one lane — strict BYOK: an
enabled workspace OpenAI ``ExternalAPIKey`` had to exist (pre-flight), and the
labeling calls refused the platform env credential (``disallow_env_fallback``,
#1242). A hosted deployment with ``ENABLE_BYOK=false`` (#1167) could therefore
never run Memory Analysis for a new workspace.

The lane resolution now has two routes, tried in this order:

1. **BYOK** — BYOK provisioning is on and an enabled OpenAI key exists for the
   workspace (context-scoped wins over workspace-scoped). Unchanged strict
   contract: ``OPENAI_FALLBACK_CHAIN`` within OpenAI, ``paid_by='byok'``,
   the platform credential is never used.
2. **Managed** — the deployment configured ``MANAGED_LLM_PROVIDER`` /
   ``MANAGED_LLM_MODEL`` and the workspace's plan carries the ``managed_llm``
   feature. Single-model chain (no cross-provider fallback), the platform
   pays (``paid_by='platform'``) and ``platform_only=True`` keeps the
   workspace's stored keys out of the call entirely.

Neither route → the same ``ValidationError`` (422 / VAL-001, ``field="byok"``)
the pre-flight always raised, with a message that names both routes.

Like ``byok_resolver`` this module holds no state and never loads or decrypts
a key: ``LLMService._get_user_api_key`` resolves the credential inside its own
coroutine frame on each call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.plan_tiers import has_feature
from config.settings import Settings, get_settings
from models.auth import ExternalAPIKey, Workspace
from services.analysis.llm_caller import OPENAI_FALLBACK_CHAIN
from services.analysis.preview import DEFAULT_MODEL_ID, DEFAULT_PROVIDER
from utils.exceptions import ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

# Plan feature that unlocks the managed lane (``config/plan_tiers.py``).
MANAGED_LLM_FEATURE = "managed_llm"

# ``memory_analyses.paid_by`` / ``sleep_reports.paid_by`` values per lane. The
# DB CHECK constraints already allow both (``MEMORY_ANALYSIS_PAID_BY_VALUES``).
PAID_BY_BYOK = "byok"
PAID_BY_PLATFORM = "platform"


@dataclass(frozen=True)
class AnalysisLane:
    """The credential + model route one analysis run labels on.

    Attributes:
        kind: ``"byok"`` or ``"managed"``.
        provider: ``LLMService`` provider key (``openai`` / ``self_hosted`` ...).
        models: Ordered fallback chain; the managed lane has exactly one.
        paid_by: Value written to ``memory_analyses.paid_by`` and the mirror
            ``sleep_reports`` row.
        platform_only: Forwarded to ``LLMService.complete_json`` — True skips
            the workspace's stored keys; False (BYOK) keeps the strict
            ``disallow_env_fallback`` contract.
    """

    kind: Literal["byok", "managed"]
    provider: str
    models: tuple[str, ...]
    paid_by: str
    platform_only: bool

    @property
    def primary_model(self) -> str:
        """First model of the chain — the one pricing and the run row record."""
        return self.models[0]


def byok_lane() -> AnalysisLane:
    """The pre-#1569 lane: strict BYOK on the OpenAI fallback chain."""
    return AnalysisLane(
        kind="byok",
        provider=DEFAULT_PROVIDER,
        models=OPENAI_FALLBACK_CHAIN,
        paid_by=PAID_BY_BYOK,
        platform_only=False,
    )


def managed_lane(provider: str, model: str) -> AnalysisLane:
    """The platform-managed lane: one model, platform-paid, stored keys ignored."""
    return AnalysisLane(
        kind="managed",
        provider=provider,
        models=(model,),
        paid_by=PAID_BY_PLATFORM,
        platform_only=True,
    )


def lane_for_run(*, paid_by: str, provider: str | None, model: str | None) -> AnalysisLane:
    """Rebuild the lane ``start()`` chose from the ``memory_analyses`` row.

    ``run()`` executes later on a fresh session, so the lane is carried by the
    row (``paid_by`` + ``llm_provider`` / ``llm_model``). Rows written before
    the e81 migration have NULL lane columns and were all BYOK.
    """
    if paid_by == PAID_BY_PLATFORM and provider and model:
        return managed_lane(provider, model)
    return byok_lane()


def _as_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


async def _openai_byok_key_exists(
    db: AsyncSession, *, workspace_id: UUID, context_id: UUID | None
) -> bool:
    """Existence check for an enabled OpenAI key (moved from ``byok_resolver``).

    Mirrors the priority chain in ``LLMService._get_user_api_key`` but stops
    at existence — does not load the row, does not decrypt anything, logs no
    key material. Context-scoped rows sort first (``ORDER BY context_id DESC
    NULLS LAST LIMIT 1``), so presence of either kind satisfies the check.
    """
    conditions = [
        ExternalAPIKey.workspace_id == workspace_id,
        ExternalAPIKey.provider == DEFAULT_PROVIDER,
        ExternalAPIKey.enabled.is_(True),
    ]
    if context_id is not None:
        conditions.append(
            or_(
                ExternalAPIKey.context_id == context_id,
                ExternalAPIKey.context_id.is_(None),
            )
        )
    else:
        conditions.append(ExternalAPIKey.context_id.is_(None))

    stmt = (
        select(ExternalAPIKey.id)
        .where(and_(*conditions))
        .order_by(ExternalAPIKey.context_id.desc().nulls_last())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


async def _workspace_plan_name(db: AsyncSession, workspace_id: UUID) -> str | None:
    result = await db.execute(select(Workspace.plan_name).where(Workspace.id == workspace_id))
    return result.scalar_one_or_none()


def _refusal_message(settings: Settings, plan_name: str | None) -> str:
    """The VAL-001 text: name every route the caller could still take."""
    if settings.enable_byok:
        byok_route = "Configure a workspace OpenAI key in External Keys settings"
    else:
        byok_route = "BYOK is disabled on this deployment"
    if not settings.managed_llm_provider:
        managed_route = (
            "or have the deployment set MANAGED_LLM_PROVIDER / MANAGED_LLM_MODEL "
            f"(the '{MANAGED_LLM_FEATURE}' plan feature then runs Memory Analysis "
            "on the managed model)"
        )
    else:
        managed_route = (
            f"or upgrade to a plan with the '{MANAGED_LLM_FEATURE}' feature — the "
            f"deployment's managed model is configured but the '{plan_name or 'free'}' "
            "plan does not include it"
        )
    return (
        "OpenAI API key not configured for this workspace. "
        f"{byok_route}, {managed_route}, before running Memory Analysis."
    )


async def resolve_analysis_lane(
    db: AsyncSession,
    *,
    workspace_id: UUID | str,
    context_id: UUID | str | None = None,
    plan_name: str | None = None,
    settings: Settings | None = None,
) -> AnalysisLane:
    """Pick the lane for a run, BYOK first, managed second, else refuse.

    Args:
        db: AsyncSession bound to the request transaction.
        workspace_id: Target workspace UUID.
        context_id: Optional context UUID (context-scoped keys win).
        plan_name: The workspace's plan when the caller already has it;
            fetched from ``workspaces`` only when the managed route is
            actually considered.
        settings: Injected for tests; defaults to ``get_settings()``.

    Returns:
        The resolved ``AnalysisLane``.

    Raises:
        ValidationError: Neither route applies (422 / VAL-001, ``field="byok"``).
    """
    settings = settings or get_settings()
    workspace_uuid = _as_uuid(workspace_id)
    context_uuid = _as_uuid(context_id) if context_id is not None else None

    if settings.enable_byok and await _openai_byok_key_exists(
        db, workspace_id=workspace_uuid, context_id=context_uuid
    ):
        logger.debug(
            "analysis_lane_resolved",
            lane="byok",
            workspace_id=str(workspace_uuid),
            context_id=str(context_uuid) if context_uuid else None,
        )
        return byok_lane()

    if settings.managed_llm_provider:
        if plan_name is None:
            plan_name = await _workspace_plan_name(db, workspace_uuid)
        if has_feature(plan_name or "", MANAGED_LLM_FEATURE):
            logger.debug(
                "analysis_lane_resolved",
                lane="managed",
                provider=settings.managed_llm_provider,
                model=settings.managed_llm_model,
                workspace_id=str(workspace_uuid),
                plan=plan_name,
            )
            return managed_lane(settings.managed_llm_provider, settings.managed_llm_model)

    raise ValidationError(
        _refusal_message(settings, plan_name),
        field="byok",
        provider=DEFAULT_PROVIDER,
        workspace_id=str(workspace_uuid),
    )


async def try_resolve_analysis_lane(
    db: AsyncSession,
    *,
    workspace_id: UUID | str,
    context_id: UUID | str | None = None,
    plan_name: str | None = None,
) -> AnalysisLane | None:
    """``resolve_analysis_lane`` for the preview path: ``None`` instead of 422.

    ``/preview`` and MCP ``dry_run`` price the model the run *would* use; a
    workspace with no lane still gets its count and a ``null`` estimate (the
    refusal belongs to ``start()``).
    """
    try:
        return await resolve_analysis_lane(
            db, workspace_id=workspace_id, context_id=context_id, plan_name=plan_name
        )
    except ValidationError:
        return None


async def preview_pricing_target(
    db: AsyncSession, *, workspace_id: UUID | str, context_id: UUID | str | None
) -> tuple[str, str]:
    """``(provider, model)`` the preview should price (#1569).

    The lane the run would take, or the BYOK default when no lane applies —
    so a workspace that will be refused at ``start()`` still sees the same
    rate card it always did.
    """
    lane = await try_resolve_analysis_lane(db, workspace_id=workspace_id, context_id=context_id)
    if lane is None:
        return DEFAULT_PROVIDER, DEFAULT_MODEL_ID
    return lane.provider, lane.primary_model
