"""Shared fixtures for the Sleep phase tests."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from services.llm_service import LLMResponse, LLMServiceError


@pytest.fixture
def failed_llm_error() -> Callable[[bool], LLMServiceError]:
    """#2038: build an ``LLMServiceError`` the way LLMService raises it.

    ``with_usage=True`` carries the usage of an attempt that answered (an
    unparseable answer, 70 tokens); ``False`` is a failure where no attempt
    answered.
    """

    def _make(with_usage: bool) -> LLMServiceError:
        usage = (
            LLMResponse(
                parsed={},
                total_tokens=70,
                input_tokens=50,
                output_tokens=20,
                cached_input_tokens=0,
                provider="openai",
                model="gpt-5-nano",
                tokenizer_version="o200k_base",
            )
            if with_usage
            else None
        )
        return LLMServiceError("unparseable", usage=usage)

    return _make
