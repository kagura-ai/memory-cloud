"""#1366 — REST ``AnalysisRow`` aggregate redaction for enforce agents (unit).

Mirrors ``tests/mcp_server/test_run_row_redaction.py`` on the REST
serialization surface: ``AnalysisRow.redacted_for_agent_scope`` must
withhold ``input_count`` / cost fields under an enforce-mode agent
scope and be a strict no-op for non-agent and shadow scopes.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from api.routes.analyses import AnalysisRow
from auth.agent_scope import AgentScope, set_agent_scope


def _row_model() -> AnalysisRow:
    return AnalysisRow(
        run_id=uuid4(),
        workspace_id=uuid4(),
        context_id=uuid4(),
        status="succeeded",
        triggered_by="user-1",
        started_at=datetime(2026, 7, 19, 0, 0, 0),
        finished_at=None,
        input_count=120,
        cost_estimated_cents=42,
        cost_actual_cents=40,
        error=None,
        cancellation_reason=None,
    )


class TestAnalysisRowRedaction:
    def teardown_method(self) -> None:
        set_agent_scope(None)

    def test_enforce_scope_withholds_aggregates(self):
        set_agent_scope(
            AgentScope(agent_id=uuid4(), enforcement_mode="enforce", workspace_id=uuid4())
        )
        out = _row_model().redacted_for_agent_scope()
        assert out.input_count is None
        assert out.cost_estimated_cents is None
        assert out.cost_actual_cents is None
        assert out.status == "succeeded"

    def test_no_scope_is_noop(self):
        set_agent_scope(None)
        row = _row_model()
        out = row.redacted_for_agent_scope()
        assert out is row
        assert out.input_count == 120

    def test_shadow_scope_is_noop(self):
        set_agent_scope(
            AgentScope(agent_id=uuid4(), enforcement_mode="shadow", workspace_id=uuid4())
        )
        out = _row_model().redacted_for_agent_scope()
        assert out.input_count == 120
        assert out.cost_actual_cents == 40
