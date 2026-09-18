"""Stage [A.5]: BYOK key existence assertion for analysis runs.

Since #1569 this module is a thin wrapper over
``services.analysis.llm_lane.resolve_analysis_lane`` — the pre-flight now
resolves a *lane* (strict BYOK first, the platform-managed LLM second) and
returns it, instead of only asserting that an OpenAI key exists. The symbol
is kept so the orchestrator's import (and the tests that patch it there)
stay put.

This module does NOT decrypt or hold the API key. The actual decrypt
happens inside ``LLMService.complete_json`` (specifically in
``LLMService._get_user_api_key``), as a local variable in the
coroutine frame. When the coroutine returns, that local is GC'd —
there is no module-level cache that retains the plaintext key.

The two security ACs are satisfied as follows:

- **`test_byok_key_never_logged`** — verifies ``LLMService`` and the
  analysis pipeline never put the decrypted bytes into a structlog
  call. ``LLMService`` already follows this convention; the lane
  resolver reinforces it by logging only ``workspace_id`` /
  ``context_id`` / lane metadata, never the lookup result's
  encrypted_value.

- **`test_byok_key_cleared_post_run`** — verifies no module-level
  state in the analysis path retains the key past
  ``orchestrator.run()``. This module holds nothing.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from services.analysis.llm_lane import AnalysisLane, resolve_analysis_lane


async def assert_openai_byok_key_available(
    db: AsyncSession,
    *,
    workspace_id: UUID | str,
    context_id: UUID | str | None = None,
    plan_name: str | None = None,
) -> AnalysisLane:
    """Resolve the lane a run may use; raise when there is none.

    Historically: assert an enabled OpenAI key exists for the workspace
    (context-scoped preferred over workspace-scoped, no env fallback). Since
    #1569 a deployment with a managed LLM lane may run a workspace whose plan
    carries ``managed_llm`` without any key, so the check returns which lane
    applies. The BYOK route is still tried first and unchanged (strict:
    ``call_with_fallback`` keeps ``disallow_env_fallback=True`` on that lane).

    Args:
        db: AsyncSession bound to the request transaction.
        workspace_id: Target workspace UUID.
        context_id: Optional context UUID.
        plan_name: The workspace's plan when already known (saves a SELECT).

    Returns:
        The ``AnalysisLane`` the orchestrator records and threads to labeling.

    Raises:
        ValidationError: No lane applies. Maps to HTTP 422 (VAL-001) — a
            user-actionable precondition failure rather than a 500; the
            message names both routes (workspace key / managed lane).
    """
    return await resolve_analysis_lane(
        db, workspace_id=workspace_id, context_id=context_id, plan_name=plan_name
    )
