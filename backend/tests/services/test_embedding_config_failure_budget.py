"""A configuration failure must not spend the embedding retry budget (#1496).

## The bug this closes

`MAX_EMBEDDING_RETRIES` is 3, and the sweep only claims a `failed` row while
`embedding_retry_count < MAX`. A row that reaches 3 is therefore never retried
again by anything. That terminal state is correct for a POISON row — bad input,
oversized text — where more attempts genuinely cannot help.

"No embedding credential is configured" was landing in the same bucket. A
workspace with no key burned all three attempts in about three minutes and then
stayed broken permanently: adding the key afterwards changed nothing, because
the rows were already at the ceiling. On the deployment that produced #1496,
467 memories sat exactly there — every one at `retry_count = 3` — saved,
counted, charged against quota, and invisible to recall in both semantic and
keyword mode, with nothing anywhere telling the owner.

## Where the weight is

`embedding_failure_values` is the decision, and it is tested here directly
against real exception instances. `process_pending_embedding` cannot be driven
in this repo without a live session, a Qdrant client and a `get_db()` loop, so
the fact that it *applies* that decision is pinned separately and narrowly at
the bottom — composition only, on the AST rather than the text.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import UTC, datetime

import pytest

from config.constants import MAX_EMBEDDING_RETRIES
from services import memory_service
from services.memory_service import (
    embedding_failure_values,
    is_configuration_failure,
)
from utils.exceptions import (
    ConfigurationError,
    EmbeddingSpendCapExceeded,
    OpenAIError,
    ValidationError,
)

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def a_spend_cap_error() -> EmbeddingSpendCapExceeded:
    return EmbeddingSpendCapExceeded("cap reached", period="daily", cap_usd=5.0, current_usd=5.0)


class TestWhichFailuresAreConfiguration:
    @pytest.mark.parametrize(
        "factory",
        [lambda: ConfigurationError("OpenAI API key not configured"), a_spend_cap_error],
        ids=["no-credential", "spend-cap"],
    )
    def test_fixable_states_are_configuration(self, factory):
        assert is_configuration_failure(factory()) is True

    @pytest.mark.parametrize(
        "factory",
        [
            lambda: OpenAIError("Embedding generation failed: connection reset"),
            lambda: ValidationError("content too long"),
            lambda: TimeoutError("read timeout"),
            lambda: RuntimeError("something else entirely"),
        ],
        ids=["provider-error", "bad-input", "timeout", "unknown"],
    )
    def test_everything_else_is_not(self, factory):
        """The poison-row backstop must survive.

        Classifying too widely would let a genuinely unfixable row retry
        forever, which is what the budget exists to prevent.
        """
        assert is_configuration_failure(factory()) is False


class TestWhatGetsStampedOnTheRow:
    def test_a_configuration_failure_refunds_the_budget(self):
        values = embedding_failure_values(ConfigurationError("no key"), NOW)
        assert values["embedding_retry_count"] == 0

    def test_a_spend_cap_failure_refunds_the_budget(self):
        """A cap is a window that rolls; the row must be alive when it does."""
        assert embedding_failure_values(a_spend_cap_error(), NOW)["embedding_retry_count"] == 0

    def test_an_ordinary_failure_leaves_the_counter_alone(self):
        """Omitting the key is what lets the claim's increment stand.

        Writing any value here — even the current one — would fight the
        counter and break the countdown to terminal.
        """
        values = embedding_failure_values(OpenAIError("boom"), NOW)
        assert "embedding_retry_count" not in values

    def test_it_resets_rather_than_decrements(self):
        """`-1` is not the inverse of the claim's increment.

        The claim adds 1 via a CASE on the pre-UPDATE status, so a refund would
        over-decrement a re-claimed stale `processing` row. 0 is exact.
        """
        assert embedding_failure_values(ConfigurationError("x"), NOW)["embedding_retry_count"] == 0

    def test_every_failure_is_marked_failed_and_stamped(self):
        for exc in (ConfigurationError("x"), OpenAIError("y"), a_spend_cap_error()):
            values = embedding_failure_values(exc, NOW)
            assert values["embedding_status"] == "failed"
            # #1317: the backoff anchors on updated_at, so it must be explicit.
            assert values["updated_at"] == NOW

    def test_the_error_is_recorded_and_bounded(self):
        """Admins need the reason; the column (String(500)) must not overrun.

        The stored text is `str(exc)`, which for this hierarchy carries a
        human prefix ("OpenAI service error: ...") — that prefix is the part an
        operator reads first, so truncation must keep the head, not the tail.
        """
        values = embedding_failure_values(OpenAIError("e" * 900), NOW)
        assert values["embedding_error"].startswith("OpenAI service error:")
        assert len(values["embedding_error"]) == 500


class TestTheExceptionTypesSurviveTheServiceLayer:
    """The load-bearing assumption, pinned where it can actually break.

    `is_configuration_failure` can only see these types if `embed_with_usage`
    re-raises them above its blanket `except Exception -> OpenAIError`. Reverse
    that ordering and the types are erased, this whole mechanism silently stops
    working, and nothing else in the suite would notice.
    """

    @staticmethod
    def _src() -> str:
        from services.embedding_service import EmbeddingService

        return inspect.getsource(EmbeddingService.embed_with_usage)

    @pytest.mark.parametrize(
        "handler", ["except ConfigurationError", "except EmbeddingSpendCapExceeded"]
    )
    def test_is_reraised_before_the_blanket_handler(self, handler):
        src = self._src()
        assert src.index(handler) < src.index("except Exception"), (
            f"{handler} now falls to the blanket handler and is remapped to "
            "OpenAIError — the type is erased before the failure handler sees "
            "it, and configuration failures start going terminal again (#1496)"
        )

    def test_neither_is_an_openai_error(self):
        """A subclass relationship would make the ordering above moot."""
        assert not issubclass(ConfigurationError, OpenAIError)
        assert not issubclass(EmbeddingSpendCapExceeded, OpenAIError)


class TestTheHandlerActuallyUsesTheDecision:
    """Composition pin, deliberately narrow.

    Everything above tests the decision in isolation. If
    `process_pending_embedding` stopped calling it — or went back to building
    the values dict inline — every test above would still pass while the bug
    returned. This is the seam those tests cannot see.
    """

    @staticmethod
    def _calls(fn) -> set[str]:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name):
                    names.add(f.id)
                elif isinstance(f, ast.Attribute):
                    names.add(f.attr)
        return names

    def test_it_delegates_to_embedding_failure_values(self):
        assert "embedding_failure_values" in self._calls(
            memory_service.process_pending_embedding
        ), (
            "process_pending_embedding no longer routes its failure UPDATE "
            "through embedding_failure_values; the retry-budget refund is "
            "unreachable (#1496)"
        )


class TestTheOperatorSignal:
    """A row entering the never-retried-again state must say so once, under a
    name distinct from the per-attempt failure that would otherwise drown it."""

    @staticmethod
    def _src() -> str:
        return inspect.getsource(memory_service.process_pending_embedding)

    def test_the_terminal_transition_has_its_own_event(self):
        assert 'logger.error("embedding_budget_exhausted"' in self._src(), (
            "nothing names the transition into a permanently unsearchable "
            "state — an operator has to query the database to find out, which "
            "is the whole of #1496"
        )

    def test_a_retryable_attempt_is_only_a_warning(self):
        """Logging every attempt at error level is why the terminal transition
        was invisible: it had no signal of its own to stand out from."""
        assert 'logger.warning("embedding_failed"' in self._src()

    def test_the_events_carry_the_error_class(self):
        """`error_class` is what turns 467 messages into one grep."""
        src = self._src()
        assert "error_class" in src
        assert "type(e).__name__" in src

    def test_the_threshold_comes_from_the_constant(self):
        """Never hardcode 3 — the sweep and this must agree by construction."""
        assert "MAX_EMBEDDING_RETRIES" in self._src()


def test_the_backfill_target_still_matches_the_ceiling():
    """The one-time #1496 migration resets rows at >= 3. If the constant moves,
    that migration is already applied and cannot follow it — a later reader
    needs to know the two were equal when it ran."""
    assert MAX_EMBEDDING_RETRIES == 3


def test_a_reset_row_gets_exactly_three_attempts():
    """Pin the number the #1496 migration docstring quotes.

    Eligibility is `retry_count < MAX`, so from 0 a row is claimed at 0, 1 and
    2 — three attempts, then it is terminal again. The first draft of that
    docstring said four; a reviewer caught it. Written down here so the prose
    and the constant cannot drift apart again.
    """
    attempts = 0
    count = 0
    while count < MAX_EMBEDDING_RETRIES:
        count += 1  # the claim increments on re-claim
        attempts += 1
    assert attempts == 3
