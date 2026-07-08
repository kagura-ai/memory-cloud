"""Stage [A.5]: BYOK key existence assertion for analysis runs.

This module does NOT decrypt or hold the API key. The actual decrypt
happens inside ``LLMService.complete_json`` (specifically in
``LLMService._get_user_api_key``), as a local variable in the
coroutine frame. When the coroutine returns, that local is GC'd —
there is no module-level cache that retains the plaintext key.

This module's job is narrower:

1. **Pre-flight assert** — confirm the workspace has an enabled OpenAI
   key BEFORE the pipeline starts compute work, so we raise
   ``ConfigurationError`` (CFG-001 / 500) at Stage [A.5] instead of
   failing at Stage [G] after spending CPU and wall-clock on
   clustering. The 422 surface is added at the API layer in #496.

2. **Centralize the lookup query** so a future BYOK extraction
   (cf. ``embedding_service._get_user_api_key`` /
   ``llm_service._get_user_api_key`` duplication) can replace this
   module without touching the orchestrator. Tracked as a follow-up
   refactor; not in scope for #495.

The two security ACs are satisfied as follows:

- **`test_byok_key_never_logged`** — verifies ``LLMService`` and the
  analysis pipeline never put the decrypted bytes into a structlog
  call. ``LLMService`` already follows this convention; this module
  reinforces it by logging only ``workspace_id`` / ``context_id``,
  never the lookup result's encrypted_value.

- **`test_byok_key_cleared_post_run`** — verifies no module-level
  state in the analysis path retains the key past
  ``orchestrator.run()``. This module holds nothing.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import ExternalAPIKey
from services.analysis.preview import DEFAULT_PROVIDER
from utils.exceptions import ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)


async def assert_openai_byok_key_available(
    db: AsyncSession,
    *,
    workspace_id: UUID | str,
    context_id: UUID | str | None = None,
) -> None:
    """Assert an enabled OpenAI key exists for the workspace.

    Mirrors the priority chain in ``LLMService._get_user_api_key``
    (lines 435-461) but stops at existence — does not load the row,
    does not decrypt anything. Logs no key material.

    Priority chain:

    1. Context-scoped: ``workspace_id`` matches AND
       ``context_id IS NOT NULL`` matches the supplied context_id.
    2. Workspace-scoped: ``workspace_id`` matches AND
       ``context_id IS NULL``.

    No env-var fallback is honored here. The analysis path requires
    an explicit BYOK row; ``OPENAI_API_KEY`` env-var is only the
    legacy dev fallback in ``LLMService`` and is not appropriate
    for a paid feature.

    Args:
        db: AsyncSession bound to the request transaction.
        workspace_id: Target workspace UUID.
        context_id: Optional context UUID. Context-scoped keys are
            preferred over workspace-scoped if both exist (see the
            ``ORDER BY context_id DESC NULLS LAST LIMIT 1`` clause).

    Raises:
        ValidationError: No enabled OpenAI key for the workspace.
            Maps directly to HTTP 422 (VAL-001) — a user-actionable
            precondition failure rather than a 500 server-config
            problem. Per the #495/#496 contract the API layer surfaces
            this with a hint pointing to the External Keys UI.
    """
    workspace_uuid = workspace_id if isinstance(workspace_id, UUID) else UUID(str(workspace_id))

    conditions = [
        ExternalAPIKey.workspace_id == workspace_uuid,
        ExternalAPIKey.provider == DEFAULT_PROVIDER,
        ExternalAPIKey.enabled.is_(True),
    ]
    if context_id is not None:
        context_uuid = context_id if isinstance(context_id, UUID) else UUID(str(context_id))
        conditions.append(
            or_(
                ExternalAPIKey.context_id == context_uuid,
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
    if result.scalar_one_or_none() is None:
        raise ValidationError(
            "OpenAI API key not configured for this workspace. "
            "Configure a workspace OpenAI key in External Keys settings "
            "before running Memory Analysis.",
            field="byok",
            provider="openai",
            workspace_id=str(workspace_uuid),
        )

    logger.debug(
        "analysis_byok_key_assertion_passed",
        workspace_id=str(workspace_uuid),
        context_id=str(context_id) if context_id else None,
    )
