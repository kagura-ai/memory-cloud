"""The total memory cap refusal (#3022): typed, with remedies and a review count.

Unit level: the typed raise in ``QuotaService.check_memory_quota``, the help
texts of ``services.plan_headroom``, the refusal count helper of
``services.memory_cap_refusal`` and the target ``MemoryService.remember``
puts on the refusal. The DB-backed half is
``tests/integration/test_3022_plan_limit_refusal_db.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from config.constants import NEVER_DELIVERED_AGE_DAYS, QUOTA_TYPES
from config.plan_tiers import PLAN_ORDER, PlanName, get_plan_tier, lowest_tier_with_limit
from models.schemas import RememberRequest
from services import memory_cap_refusal as mcr
from services import plan_headroom as ph
from utils.exceptions import QuotaExceededError

TOP = PLAN_ORDER[-1]


def _execute_returning(*results: object) -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock(side_effect=list(results))
    return db


def _workspace_result(workspace: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = workspace
    return result


async def _total_cap(plan: str = PlanName.FREE, *, billing: bool = True) -> QuotaExceededError:
    from services.quota_service import QuotaService

    count = MagicMock()
    count.scalar.return_value = 1000
    db = _execute_returning(_workspace_result(SimpleNamespace(plan_name=plan)), count)
    effective = MagicMock(get_effective_quotas=AsyncMock(return_value={"memory_limit": 1000}))
    with (
        patch("services.quota_service.EffectiveQuotaService", return_value=effective),
        patch("services.plan_headroom.billing_configured", return_value=billing),
        pytest.raises(QuotaExceededError) as exc,
    ):
        await QuotaService(db).check_memory_quota(uuid4(), raise_on_exceeded=True)
    return exc.value


# ---------------------------------------------------------------- the typed raise


class TestTypedTotalCap:
    def test_memories_is_in_the_frozen_vocabulary(self) -> None:
        assert "memories" in QUOTA_TYPES

    @pytest.mark.asyncio
    async def test_the_total_cap_is_a_typed_quota_gate(self) -> None:
        exc = await _total_cap()

        details = exc.details
        assert details["gate"] == "quota"
        assert details["quota_type"] == "memories"
        assert (details["current"], details["limit"]) == (1000, 1000)
        assert details["current_plan"] == PlanName.FREE
        base = get_plan_tier(PlanName.FREE).memory_limit
        assert details["required_plan"] == lowest_tier_with_limit("memory_limit", base)
        assert details["required_plan"] is not None
        assert details["remedies"] == ["cleanup", "upgrade"]
        # The message keeps the prefix clients and tests read.
        assert exc.message == (
            "Memory quota exceeded. Current: 1000, Limit: 1000 (free plan + addons)"
        )

    @pytest.mark.asyncio
    async def test_without_billing_the_remedy_is_the_administrator(self) -> None:
        exc = await _total_cap(billing=False)
        assert exc.details["remedies"] == ["cleanup", "admin"]

    @pytest.mark.asyncio
    async def test_on_the_top_tier_no_upgrade_is_offered(self) -> None:
        exc = await _total_cap(TOP)
        assert exc.details["remedies"] == ["cleanup"]
        assert exc.details["required_plan"] is None

    @pytest.mark.asyncio
    async def test_a_missing_workspace_stays_untyped(self) -> None:
        from services.quota_service import QuotaService

        db = _execute_returning(_workspace_result(None))
        with pytest.raises(QuotaExceededError) as exc:
            await QuotaService(db).check_memory_quota(uuid4(), raise_on_exceeded=True)
        assert exc.value.details == {"quota_type": None}
        assert mcr.memory_cap_target(exc.value) is None
        assert not mcr.is_memory_cap(exc.value)


# ---------------------------------------------------------------- the help texts


def _sentence(n: int) -> str:
    return ph.never_delivered_sentence(n)


class TestHelpTexts:
    def test_the_count_sentence_names_the_threshold_and_agrees_in_number(self) -> None:
        assert _sentence(3) == (
            f"3 of your memories in this context are over {NEVER_DELIVERED_AGE_DAYS} days old "
            "and were never delivered."
        )
        assert _sentence(1) == (
            f"1 of your memories in this context is over {NEVER_DELIVERED_AGE_DAYS} days old "
            "and was never delivered."
        )

    def test_with_a_count_the_help_says_how_to_review_then_the_plan(self) -> None:
        text = ph.memory_cap_help(3, ["cleanup", "upgrade"])
        assert text.startswith(_sentence(3) + " Review them with list(context_id, ")
        assert "filters={'never_delivered': true}" in text
        assert "with the user's approval" in text
        assert "memory_ids takes up to 50" in text
        assert text.endswith(
            "Or upgrade the plan from the workspace plan page in the web UI "
            "(Workspace settings → Plan)."
        )
        admin = ph.memory_cap_help(3, ["cleanup", "admin"])
        assert admin.endswith("Or ask the deployment's administrator to raise the plan's limit.")
        top = ph.memory_cap_help(3, ["cleanup"])
        assert top.endswith("(memory_ids takes up to 50).")
        assert " Or " not in top

    @pytest.mark.parametrize("count", [0, None])
    def test_without_a_count_the_help_is_the_remedy_list(self, count) -> None:
        text = ph.memory_cap_help(count, ["cleanup", "upgrade"])
        assert text == (
            "Free capacity: forget memories the user no longer needs (memory_ids takes up to "
            "50) or delete unused contexts, or upgrade the plan from the workspace plan page "
            "in the web UI (Workspace settings → Plan)."
        )
        assert "never delivered" not in text
        assert "At 100 %" not in text

    @pytest.mark.parametrize("count", [0, 1, 7, None])
    @pytest.mark.parametrize(
        "remedies", [["cleanup", "upgrade"], ["cleanup", "admin"], ["cleanup"]]
    )
    def test_the_neutral_text_names_no_tool_and_is_never_longer(self, count, remedies) -> None:
        standard = ph.memory_cap_help(count, remedies)
        neutral = ph.memory_cap_help(count, remedies, neutral=True)
        for name in ("list(", "forget(", "memory_ids", "filters="):
            assert name not in neutral
        assert len(neutral) <= len(standard)
        if count:
            assert neutral.startswith(_sentence(count))

    def test_the_neutral_help_is_rebuilt_from_the_reply(self) -> None:
        payload = {"remedies": ["cleanup", "upgrade"], "never_delivered_count": 2, "help": "x"}
        assert ph.memory_cap_neutral_help(payload) == ph.memory_cap_help(
            2, ["cleanup", "upgrade"], neutral=True
        )
        no_count = {"remedies": ["cleanup", "admin"], "help": "x"}
        assert ph.memory_cap_neutral_help(no_count) == ph.memory_cap_help(
            None, ["cleanup", "admin"], neutral=True
        )
        # Not this refusal: a daily cap's remedies, an unknown token, no list.
        assert ph.memory_cap_neutral_help({"remedies": ["wait"]}) is None
        assert ph.memory_cap_neutral_help({"remedies": ["cleanup", "bogus"]}) is None
        assert ph.memory_cap_neutral_help({"remedies": "cleanup"}) is None
        assert ph.memory_cap_neutral_help({}) is None


# ---------------------------------------------------------------- the refusal count


def _typed(target: mcr.MemoryCapTarget | None = None) -> QuotaExceededError:
    from config.plan_tiers import quota_gate_details

    exc = QuotaExceededError(
        "Memory quota exceeded. Current: 5, Limit: 5 (free plan + addons)",
        **quota_gate_details(PlanName.FREE, "memories", current=5, limit=5),
        remedies=["cleanup", "upgrade"],
    )
    if target is not None:
        mcr.set_memory_cap_target(exc, target)
    return exc


def _db() -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock()
    db.rollback = AsyncMock()
    return db


class TestRefusalCount:
    def test_only_the_typed_total_cap_is_a_memory_cap(self) -> None:
        from config.plan_tiers import quota_gate_details

        assert mcr.is_memory_cap(_typed())
        daily = QuotaExceededError(
            "Daily memory-creation quota exceeded.",
            **quota_gate_details(PlanName.FREE, "memories_per_day", current=5, limit=5),
        )
        assert not mcr.is_memory_cap(daily)
        assert not mcr.is_memory_cap(QuotaExceededError("Memory quota exceeded."))
        assert not mcr.is_memory_cap(ValueError("x"))

    @pytest.mark.asyncio
    async def test_no_target_no_count(self) -> None:
        db = _db()
        with patch.object(mcr, "count_never_delivered", new=AsyncMock()) as count:
            assert await mcr.refusal_never_delivered_count(db, _typed(), user_id="u") is None
        count.assert_not_awaited()
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("is_private", [False, True])
    async def test_the_count_is_the_callers_in_the_target_context(self, is_private) -> None:
        target = mcr.MemoryCapTarget(context_id=uuid4(), is_private=is_private)
        db = _db()
        with patch.object(mcr, "count_never_delivered", new=AsyncMock(return_value=4)) as count:
            n = await mcr.refusal_never_delivered_count(db, _typed(target), user_id="alice")

        assert n == 4
        count.assert_awaited_once()
        kwargs = count.await_args.kwargs
        assert kwargs["context_id"] == target.context_id
        assert kwargs["viewer_user_id"] == "alice"
        assert kwargs["owner_user_id"] == ("alice" if is_private else None)
        # A server-side statement timeout bounds the count, and the read
        # transaction ends after it.
        db.execute.assert_awaited_once()
        db.rollback.assert_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_count_is_absent_and_logged(self) -> None:
        target = mcr.MemoryCapTarget(context_id=uuid4(), is_private=False)
        db = _db()
        with (
            patch.object(mcr, "count_never_delivered", new=AsyncMock(side_effect=RuntimeError)),
            patch.object(mcr, "logger") as log,
        ):
            assert await mcr.refusal_never_delivered_count(db, _typed(target), user_id="u") is None
        log.warning.assert_called_once()
        assert log.warning.call_args.args == ("memory_cap_refusal_count_unavailable",)
        db.rollback.assert_awaited()

    @pytest.mark.asyncio
    async def test_a_slow_count_is_absent(self) -> None:
        target = mcr.MemoryCapTarget(context_id=uuid4(), is_private=False)
        db = _db()

        async def slow(*_a, **_k):
            await asyncio.sleep(5)
            return 1

        with (
            patch.object(mcr, "count_never_delivered", new=slow),
            patch.object(mcr, "COUNT_TIMEOUT_SECONDS", 0.01),
            patch.object(mcr, "logger") as log,
        ):
            assert await mcr.refusal_never_delivered_count(db, _typed(target), user_id="u") is None
        assert log.warning.call_args.kwargs["error_class"] == "TimeoutError"

    @pytest.mark.asyncio
    async def test_a_failing_rollback_after_a_failure_is_swallowed(self) -> None:
        target = mcr.MemoryCapTarget(context_id=uuid4(), is_private=False)
        db = _db()
        db.rollback = AsyncMock(side_effect=RuntimeError("connection gone"))
        with patch.object(mcr, "count_never_delivered", new=AsyncMock(side_effect=RuntimeError)):
            assert await mcr.refusal_never_delivered_count(db, _typed(target), user_id="u") is None

    @pytest.mark.parametrize(
        ("count", "suffix"),
        [(None, ""), (0, ""), (2, ". " + ph.never_delivered_sentence(2))],
    )
    def test_the_message_gains_the_sentence_only_above_zero(self, count, suffix) -> None:
        # The base ends in the plan parenthesis without a full stop: the count
        # is its own sentence after one.
        base = "Memory quota exceeded. Current: 5, Limit: 5 (free plan + addons)"
        assert mcr.refusal_message(base, count) == base + suffix

    def test_a_base_that_ends_a_sentence_gets_no_second_full_stop(self) -> None:
        base = "Memory quota exceeded."
        assert mcr.refusal_message(base, 1) == f"{base} {ph.never_delivered_sentence(1)}"


# ---------------------------------------------------------------- the target


@pytest.mark.asyncio
@pytest.mark.parametrize("is_private", [False, True])
async def test_remember_names_the_target_context_on_the_refusal(is_private) -> None:
    from services.memory_service import MemoryService

    service = MemoryService(_execute_returning())
    context = MagicMock()
    context.id = uuid4()
    context.workspace_id = uuid4()
    context.is_private = is_private
    service._get_context_isolation_params = AsyncMock(  # type: ignore[method-assign]
        return_value=(context, str(context.workspace_id), str(context.id))
    )
    refusing = MagicMock(
        check_memory_quota=AsyncMock(side_effect=_typed()),
        check_memories_per_day=AsyncMock(return_value=(True, None)),
    )
    with (
        patch("services.quota_service.QuotaService", return_value=refusing),
        pytest.raises(QuotaExceededError) as exc,
    ):
        await service.remember(
            RememberRequest(summary="a summary long enough", content="c", type="note"),
            user_id="u",
            current_context_id=context.id,
        )

    assert mcr.memory_cap_target(exc.value) == mcr.MemoryCapTarget(
        context_id=context.id, is_private=is_private
    )
    # The daily gate is never reached once the total cap refused.
    refusing.check_memories_per_day.assert_not_awaited()
