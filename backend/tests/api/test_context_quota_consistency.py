"""The context cap must be ONE number, everywhere (#1487).

A Pro workspace with three contexts could not create a fourth from the web UI.
The plan was right and the quota was nowhere near reached; the client had
re-implemented the cap as ``plan_name == "free" and count >= 1`` because the
API never told it what the real cap was.

Two properties keep that from coming back:

1. ``WorkspaceResponse`` carries ``max_contexts``, so no client has to guess.
2. Every cap site resolves through ``Workspace.effective_max_contexts``. The
   create check used to add ``plan base + addon`` by hand, which skips
   ``_zero_floor`` and could therefore disagree with the number the API reports.

These are unit-level on purpose: they pin the ARITHMETIC and the schema, which
is where the divergence lived. No DB, no HTTP.
"""

from __future__ import annotations

import inspect

import pytest

from api.routes.workspaces import WorkspaceResponse
from config.plan_tiers import get_plan_tier
from models.auth import Workspace
from services import quota_service


class TestResponseCarriesTheCap:
    def test_workspace_response_has_max_contexts(self):
        """The field the client reads must exist and be required."""
        assert "max_contexts" in WorkspaceResponse.model_fields
        field = WorkspaceResponse.model_fields["max_contexts"]
        assert field.annotation is int
        # Required, not defaulted: a silent 0/None would re-teach clients to
        # guess, which is the bug.
        assert field.is_required(), "max_contexts must be populated by every handler"

    def test_every_handler_populates_it(self):
        """A handler that forgets it would 500 at runtime, not fail a test."""
        source = inspect.getsource(__import__("api.routes.workspaces", fromlist=["x"]))
        constructions = source.count("WorkspaceResponse(")
        populated = source.count("max_contexts=workspace.effective_max_contexts")
        # -1 for the class definition line `class WorkspaceResponse(BaseModel)`.
        assert populated == constructions - 1, (
            f"{constructions - 1} WorkspaceResponse constructions but "
            f"{populated} populate max_contexts"
        )


class TestEffectiveCapArithmetic:
    """`effective_max_contexts` is the single source of truth."""

    @pytest.mark.parametrize(
        ("plan", "expected"),
        [("free", 1), ("basic", 3), ("pro", 20)],
    )
    def test_base_matches_the_tier(self, plan, expected):
        ws = Workspace(name="w", plan_name=plan, addon_context_bonus=0)
        assert ws.effective_max_contexts == expected
        assert ws.effective_max_contexts == get_plan_tier(plan).max_contexts_per_workspace

    def test_addon_adds_to_a_nonzero_base(self):
        ws = Workspace(name="w", plan_name="pro", addon_context_bonus=50)
        assert ws.effective_max_contexts == 70

    def test_a_pro_workspace_is_not_capped_at_one(self):
        """The reported symptom, stated as an assertion."""
        ws = Workspace(name="w", plan_name="pro", addon_context_bonus=0)
        assert ws.effective_max_contexts > 1
        # ...and three existing contexts must leave room.
        assert 3 < ws.effective_max_contexts


class TestCreateCheckUsesTheSameNumber:
    def test_quota_service_reads_the_property_not_raw_addition(self):
        """Pin the mechanism, because the two agree by luck on most tiers.

        `plan.max_contexts_per_workspace + bonus` and `effective_max_contexts`
        differ only when the base is 0 — rare, and therefore exactly the case a
        value-only test would miss on every tier we normally exercise.

        Scoped to the FUNCTION, not the module: a module-wide string search
        passes when the phrase appears in an unrelated helper or a comment,
        which is precisely how a source-matching guard goes vacuous.
        """
        source = inspect.getsource(quota_service.QuotaService.check_context_creation_allowed)
        # Strip comments so prose about the property cannot satisfy the check.
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        assert "workspace.effective_max_contexts" in code, (
            "the create check must resolve the cap through the model property"
        )
        assert "max_contexts_per_workspace" not in code, (
            "raw addition bypasses _zero_floor and can disagree with the API"
        )

    @pytest.mark.asyncio
    async def test_enforced_cap_equals_the_reported_cap(self):
        """Behavioural: the number enforced IS `effective_max_contexts`.

        The source check above pins how; this pins what. Together they survive
        a refactor that keeps the value but changes the expression, and a
        refactor that keeps the expression but changes the value.
        """
        from unittest.mock import AsyncMock, MagicMock

        ws = Workspace(name="w", plan_name="pro", addon_context_bonus=0)
        ws.id = "11111111-1111-1111-1111-111111111111"

        svc = quota_service.QuotaService(db=MagicMock())

        # First execute() resolves the workspace, second returns the count.
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        count_result = MagicMock()
        # Exactly at the cap -> must deny; one below -> must allow.
        count_result.scalar.return_value = ws.effective_max_contexts
        svc.db.execute = AsyncMock(side_effect=[ws_result, count_result])

        allowed, error = await svc.check_context_creation_allowed(ws.id, raise_on_denied=False)
        assert allowed is False
        assert error is not None
        # The message must quote the SAME cap the API reports, or the UI and the
        # error text disagree — which is how this was misread as a plan problem.
        assert str(ws.effective_max_contexts) in error

        count_result.scalar.return_value = ws.effective_max_contexts - 1
        svc.db.execute = AsyncMock(side_effect=[ws_result, count_result])
        allowed, _ = await svc.check_context_creation_allowed(ws.id, raise_on_denied=False)
        assert allowed is True

    @pytest.mark.asyncio
    async def test_a_pro_workspace_with_three_contexts_is_allowed(self):
        """The exact reported state, end to end through the real check."""
        from unittest.mock import AsyncMock, MagicMock

        ws = Workspace(name="personal", plan_name="pro", addon_context_bonus=0)
        ws.id = "22222222-2222-2222-2222-222222222222"

        svc = quota_service.QuotaService(db=MagicMock())
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        count_result = MagicMock()
        count_result.scalar.return_value = 3
        svc.db.execute = AsyncMock(side_effect=[ws_result, count_result])

        allowed, error = await svc.check_context_creation_allowed(ws.id, raise_on_denied=False)
        assert allowed is True, f"pro/3 contexts must be allowed, got: {error}"

    def test_zero_base_cannot_be_lifted_by_an_addon(self):
        """What the raw addition got wrong, as a value.

        A tier with no context entitlement must stay at zero even if an addon
        bonus is present; otherwise an addon silently grants access the plan
        does not include.
        """
        ws = Workspace(name="w", plan_name="free", addon_context_bonus=5)
        # free base is 1, so this only demonstrates addition; the zero-base
        # branch is what _zero_floor guards. Assert the helper's contract
        # directly so the guarantee is pinned regardless of tier values.
        from models.auth import _zero_floor

        assert _zero_floor(0, 5) == 0
        assert _zero_floor(1, 5) == 6
        assert ws.effective_max_contexts == _zero_floor(1, 5)
