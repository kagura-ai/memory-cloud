"""Tests for the analysis LLM caller adapter (Stage [G]).

Pins three Phase-4 design contracts:

1. ``test_fallback_stays_within_openai`` — when the primary
   gpt-5-nano fails, the adapter retries gpt-5.5 and NEVER advances
   to a non-OpenAI provider. This is the AC-pinned cross-provider
   prohibition.

2. ``test_502_wrapping_after_chain_exhausted`` — when both models
   in the fallback chain fail, the adapter raises
   ``AnalysisLLMUpstreamError`` (status_code=502 with
   ``upstream_provider_error=True`` detail) — NOT the bare
   ``LLMServiceError`` which would 500.

3. ``test_filter_hallucinated_ids`` — the frozenset guard returns
   only members of the requested set, in input order. Mirrors
   ``services/sleep/edge_discovery.py:818`` pattern.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services.analysis.llm_caller import (
    OPENAI_FALLBACK_CHAIN,
    AnalysisLLMUpstreamError,
    call_with_fallback,
    filter_hallucinated_ids,
)
from services.llm_service import LLMResponse, LLMServiceError


def _ok_response(model: str) -> LLMResponse:
    """Build a minimal LLMResponse for happy-path mocks."""
    return LLMResponse(
        parsed={"label": "ok", "description": "ok desc", "label_confidence": 0.9},
        total_tokens=120,
        input_tokens=80,
        output_tokens=40,
        cached_input_tokens=0,
        provider="openai",
        model=model,
    )


@pytest.mark.asyncio
async def test_chain_constants_are_openai_only() -> None:
    """The default fallback chain stays within OpenAI by construction.

    Pin the constant so a future code-search like ``grep gemini``
    inside this file would highlight any drift.
    """
    assert OPENAI_FALLBACK_CHAIN == ("gpt-5-nano", "gpt-5.5")
    # Both models are OpenAI per c03_471_seed_pricing.
    for model in OPENAI_FALLBACK_CHAIN:
        assert model.startswith("gpt-")


@pytest.mark.asyncio
async def test_primary_succeeds_no_fallback_attempted() -> None:
    """Happy path: primary returns OK, fallback is never called."""
    llm_service = AsyncMock()
    llm_service.complete_json = AsyncMock(return_value=_ok_response("gpt-5-nano"))

    result = await call_with_fallback(
        llm_service,
        user_id="u1",
        workspace_id="w1",
        context_id=None,
        system_prompt="sys",
        prompt="p",
    )

    assert result.response.model == "gpt-5-nano"
    assert llm_service.complete_json.call_count == 1


@pytest.mark.asyncio
async def test_fallback_stays_within_openai() -> None:
    """When primary fails, the adapter retries the next OpenAI model.

    The mock complete_json sees the calls in order; we assert the
    second call's ``model`` kwarg is from ``OPENAI_FALLBACK_CHAIN``,
    NOT a non-OpenAI provider. The test fails if a future contributor
    edits the chain to include a cross-provider fallback.
    """
    primary_error = LLMServiceError("primary unavailable")
    llm_service = AsyncMock()
    llm_service.complete_json = AsyncMock(side_effect=[primary_error, _ok_response("gpt-5.5")])

    result = await call_with_fallback(
        llm_service,
        user_id="u1",
        workspace_id="w1",
        context_id=None,
        system_prompt="sys",
        prompt="p",
    )

    assert result.response.model == "gpt-5.5"
    assert llm_service.complete_json.call_count == 2
    # Both calls must specify provider='openai'. A non-openai value
    # in either call would mean cross-provider fallback was attempted.
    for call in llm_service.complete_json.call_args_list:
        assert call.kwargs["provider"] == "openai", (
            f"Cross-provider fallback detected: {call.kwargs}"
        )


@pytest.mark.asyncio
async def test_502_wrapping_after_chain_exhausted() -> None:
    """All models in the fallback chain fail → AnalysisLLMUpstreamError(502)."""
    llm_service = AsyncMock()
    llm_service.complete_json = AsyncMock(
        side_effect=[
            LLMServiceError("nano down"),
            LLMServiceError("5.5 down"),
        ]
    )

    with pytest.raises(AnalysisLLMUpstreamError) as excinfo:
        await call_with_fallback(
            llm_service,
            user_id="u1",
            workspace_id="w1",
            context_id=None,
            system_prompt="sys",
            prompt="p",
        )

    err = excinfo.value
    assert err.status_code == 502
    assert err.error_code == "EXT-ANA-001"
    assert err.details.get("upstream_provider_error") is True
    assert err.details.get("attempted_models") == list(OPENAI_FALLBACK_CHAIN)
    # The exception's message must not include the BYOK key — but
    # since the mock didn't pass one, this just confirms the
    # message is bounded.
    assert "All fallback models exhausted" in str(err)


@pytest.mark.asyncio
async def test_failed_attempt_billed_tokens_are_accumulated() -> None:
    """#1247: a failed fallback attempt that still burned paid tokens must
    have its usage carried forward so cost reflects BOTH calls.

    The primary model fails *after* the provider round-trip completed
    (unparseable JSON on both internal tries) — the ``LLMServiceError``
    carries that call's usage via ``response``. The fallback model then
    succeeds. The returned ``CallResult`` exposes the failed usage in
    ``prior_usages``, and the labeler's breakdown accumulation sums both.
    """
    failed_usage = LLMResponse(
        parsed={},
        total_tokens=90,
        input_tokens=60,
        output_tokens=30,
        cached_input_tokens=0,
        provider="openai",
        model="gpt-5-nano",
    )
    primary_error = LLMServiceError("nano returned unparseable json twice")
    # Real ``LLMServiceError`` is a plain Exception; the caller reads an
    # optional ``response`` carrying the billed usage of the failed call.
    primary_error.response = failed_usage  # type: ignore[attr-defined]

    llm_service = AsyncMock()
    llm_service.complete_json = AsyncMock(side_effect=[primary_error, _ok_response("gpt-5.5")])

    result = await call_with_fallback(
        llm_service,
        user_id="u1",
        workspace_id="w1",
        context_id=None,
        system_prompt="sys",
        prompt="p",
    )

    # Winner is the fallback model; the failed attempt's usage is preserved.
    assert result.response.model == "gpt-5.5"
    assert len(result.prior_usages) == 1
    assert result.prior_usages[0].input_tokens == 60
    assert result.prior_usages[0].output_tokens == 30

    # The recorded cost breakdown (the same accumulation the labeler does)
    # must reflect BOTH calls, not just the winning response.
    from services.sleep.reporter import accumulate_llm_response

    breakdown = accumulate_llm_response(None, result.response)
    for prior in result.prior_usages:
        breakdown.add_call(
            input_tokens=prior.input_tokens,
            output_tokens=prior.output_tokens,
            cached_input_tokens=prior.cached_input_tokens,
        )
    assert breakdown.calls == 2
    # winner (_ok_response) is 80 in / 40 out; failed attempt adds 60 / 30.
    assert breakdown.input_tokens == 80 + 60
    assert breakdown.output_tokens == 40 + 30


@pytest.mark.asyncio
async def test_failed_attempt_without_usage_is_skipped() -> None:
    """A failed attempt that exposes no usage (transport error before any
    completion) contributes nothing — prior_usages stays empty."""
    llm_service = AsyncMock()
    llm_service.complete_json = AsyncMock(
        side_effect=[LLMServiceError("connection refused"), _ok_response("gpt-5.5")]
    )

    result = await call_with_fallback(
        llm_service,
        user_id="u1",
        workspace_id="w1",
        context_id=None,
        system_prompt="sys",
        prompt="p",
    )

    assert result.response.model == "gpt-5.5"
    assert result.prior_usages == ()


def test_filter_hallucinated_ids_drops_non_members() -> None:
    """The frozenset guard returns only members, preserving input order."""
    requested = frozenset({"id-1", "id-2", "id-3"})
    candidates = ["id-2", "id-fake", "id-1", "id-also-fake"]

    out = filter_hallucinated_ids(candidates, requested)

    # Order preserved; only members kept.
    assert out == ["id-2", "id-1"]


def test_filter_hallucinated_ids_empty_input() -> None:
    """Empty candidate list returns empty list."""
    requested = frozenset({"id-1"})
    assert filter_hallucinated_ids([], requested) == []


def test_filter_hallucinated_ids_all_hallucinated() -> None:
    """All candidates outside the requested set returns empty list."""
    requested = frozenset({"id-1"})
    assert filter_hallucinated_ids(["id-evil-1", "id-evil-2"], requested) == []


@pytest.mark.asyncio
async def test_calls_pass_strict_byok_flag() -> None:
    """#1242: the analysis wrapper is the paid-feature path — every
    ``complete_json`` call must request strict BYOK resolution so a
    mid-run key removal cannot fall back to the platform env key."""
    llm_service = AsyncMock()
    llm_service.complete_json = AsyncMock(return_value=_ok_response("gpt-5-nano"))

    await call_with_fallback(
        llm_service,
        user_id="u1",
        workspace_id="w1",
        context_id=None,
        system_prompt="sys",
        prompt="p",
    )

    for call in llm_service.complete_json.call_args_list:
        assert call.kwargs.get("disallow_env_fallback") is True


@pytest.mark.asyncio
async def test_configuration_error_propagates_without_fallback() -> None:
    """#1242: ConfigurationError (BYOK row gone mid-run) is NOT an
    upstream provider failure — it must escape immediately and fail the
    run, not burn the remaining fallback-chain attempts."""
    from utils.exceptions import ConfigurationError

    llm_service = AsyncMock()
    llm_service.complete_json = AsyncMock(
        side_effect=ConfigurationError("openai API key not configured (BYOK required)")
    )

    with pytest.raises(ConfigurationError):
        await call_with_fallback(
            llm_service,
            user_id="u1",
            workspace_id="w1",
            context_id=None,
            system_prompt="sys",
            prompt="p",
        )
    assert llm_service.complete_json.call_count == 1
