"""Stage [G] LLM caller adapter for cluster labeling.

Wraps ``LLMService.complete_json`` with three analysis-specific concerns:

1. **Within-provider fallback**: ``gpt-5-nano`` → ``gpt-5.5``
   (next-cheapest OpenAI). Cross-provider fallback is FORBIDDEN —
   the BYOK key is provider-scoped, and a "smart cost-saving"
   fallback to a different provider would break the BYOK contract.
   The fallback chain is a tuple constant; future v1.5 work that
   adds Gemini will swap this for a provider-keyed mapping.

2. **502 wrapping**: ``LLMServiceError`` (which is a plain
   ``Exception`` and does not auto-map to an HTTP status) is
   re-raised as ``AnalysisLLMUpstreamError`` (a subclass of
   ``ExternalServiceError`` with ``status_code=502``) once the
   entire fallback chain is exhausted. The detail dict carries
   ``upstream_provider_error=True`` so the API layer (#496) can
   distinguish this from auth / quota / config failures, and
   ``attempted_models`` so observability can attribute the failure
   to a specific model in the chain.

3. **Frozenset hallucination guard**: callers pass a
   ``requested_set`` of valid memory_id strings; returned
   representatives are filtered against this set so a misbehaving
   model cannot inject memory_ids that were never in the cluster.
   Pattern mirrors ``services/sleep/edge_discovery.py:818``
   (orientation-agnostic).

The Semaphore for parallel=8 dispatch lives at the call site
(``labeler.py``) because the labeler owns the lifetime of the
``asyncio.gather`` over clusters.

Deletability: this module is **deletable** when ``LLMService`` gains
a native ``fallback_chain`` config and a 502-wrapping path. At that
point the labeler can call ``LLMService.complete_json`` directly with
a ``fallback_chain=...`` kwarg, and only the frozenset guard remains
(which can move into ``labeler.py`` as 5 lines). Tracked as v1.5
follow-up. The constant ``OPENAI_FALLBACK_CHAIN`` and the helper
``filter_hallucinated_ids`` will move with the deletion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.llm_service import LLMResponse, LLMService, LLMServiceError
from utils.exceptions import ExternalServiceError
from utils.logger import get_logger

logger = get_logger(__name__)


# OpenAI provider fallback chain. Listed primary-then-fallback so the
# first attempt is the cost target (gpt-5-nano per Phase 3 default);
# the fallback fires only on upstream provider failure, never on
# logic errors. Both models have ``llm_pricing`` rows seeded by
# ``c03_471_seed_pricing`` so cost rows can resolve either choice.
# Cross-provider entries are forbidden — provider stays "openai".
OPENAI_FALLBACK_CHAIN: tuple[str, ...] = ("gpt-5-nano", "gpt-5.5")


class AnalysisLLMUpstreamError(ExternalServiceError):
    """All fallback models in the OpenAI chain failed (502).

    Issued by ``call_with_fallback`` when every model in
    ``OPENAI_FALLBACK_CHAIN`` raised ``LLMServiceError`` (upstream
    provider failure or unrecoverable JSON parse). The 502 status
    plus ``upstream_provider_error=True`` lets the API layer (#496)
    return a clean "the model provider is down" response instead of
    a generic 500.

    Tracked as ``error_code='EXT-ANA-001'`` in the
    ``MemoryCloudException`` hierarchy — distinct from
    ``OpenAIError`` (EXT-201) which is used by other services for
    direct OpenAI errors that did not exhaust a fallback chain.
    """

    def __init__(self, message: str, attempted_models: list[str]) -> None:
        super().__init__(
            "OpenAI",
            message,
            error_code="EXT-ANA-001",
            upstream_provider_error=True,
            attempted_models=attempted_models,
        )


@dataclass(frozen=True)
class CallResult:
    """Result of one labeling call.

    ``response.model`` identifies which model in the fallback chain
    actually produced the response — the labeler uses this to write
    one ``LLMCallBreakdown`` per (provider, model) pair into
    ``sleep_report_llm_usage``.
    """

    parsed: dict[str, Any]
    response: LLMResponse


async def call_with_fallback(
    llm_service: LLMService,
    *,
    user_id: str,
    workspace_id: str,
    context_id: str | None,
    system_prompt: str,
    prompt: str,
    fallback_chain: tuple[str, ...] = OPENAI_FALLBACK_CHAIN,
    temperature: float = 0.1,
    max_tokens: int = 1024,
) -> CallResult:
    """Call ``LLMService.complete_json`` with within-OpenAI fallback.

    Tries each model in ``fallback_chain`` in order. On
    ``LLMServiceError`` (upstream provider failure or post-retry
    JSON parse failure), advances to the next model. If all models
    in the chain fail, raises ``AnalysisLLMUpstreamError`` with the
    last upstream error message attached.

    Args:
        llm_service: Resolved ``LLMService`` instance.
        user_id: Triggering user (used for logging only — see #385).
        workspace_id: Workspace UUID (string form, used by
            ``LLMService._get_user_api_key`` for BYOK lookup).
        context_id: Optional context UUID (string form).
        system_prompt: System message.
        prompt: User message.
        fallback_chain: Ordered tuple of OpenAI models to try. MUST
            be same-provider; cross-provider fallback is forbidden.
            Default ``OPENAI_FALLBACK_CHAIN``.
        temperature: Sampling temperature.
        max_tokens: Output cap.

    Returns:
        ``CallResult(parsed, response)`` where ``response.model``
        identifies which model in the chain produced the result.

    Raises:
        AnalysisLLMUpstreamError: All models in ``fallback_chain``
            failed. ``status_code=502``,
            ``details={'upstream_provider_error': True,
            'attempted_models': [...]}``.
    """
    last_error: LLMServiceError | None = None
    for model in fallback_chain:
        try:
            response = await llm_service.complete_json(
                user_id=user_id,
                prompt=prompt,
                system_prompt=system_prompt,
                workspace_id=workspace_id,
                context_id=context_id,
                model=model,
                provider="openai",
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return CallResult(parsed=response.parsed, response=response)
        except LLMServiceError as e:
            logger.warning(
                "analysis_llm_call_failed",
                model=model,
                error=str(e),
            )
            last_error = e

    raise AnalysisLLMUpstreamError(
        message=(
            f"All fallback models exhausted: {list(fallback_chain)}. Last error: {last_error}"
        ),
        attempted_models=list(fallback_chain),
    )


def filter_hallucinated_ids(
    candidate_ids: list[str],
    requested_set: frozenset[str],
) -> list[str]:
    """Drop ids the LLM invented that weren't in the requested set.

    Returns candidates that are members of ``requested_set``,
    preserving input order. Mirrors the frozenset guard pattern in
    ``services/sleep/edge_discovery.py:818`` — orientation-agnostic
    membership test, drops without raising so one bad id does not
    abort parsing of the entire response.
    """
    return [c for c in candidate_ids if c in requested_set]
