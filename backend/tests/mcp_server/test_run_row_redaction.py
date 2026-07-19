"""#1366 — run-row aggregate redaction + cap-message redaction (unit).

``_serialize_run_row`` (MCP) and ``assert_run_size_within_cap`` are pure
with respect to the DB: the former reads only the row object and the
agent-scope contextvar, the latter only settings. Both redactions are
exercised here without a database.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from auth.agent_scope import AgentScope, set_agent_scope
from mcp_server.tools.analysis import _serialize_run_row
from services.analysis.preview import assert_run_size_within_cap
from utils.exceptions import ValidationError


def _row() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
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


def _enforce_scope() -> None:
    set_agent_scope(AgentScope(agent_id=uuid4(), enforcement_mode="enforce", workspace_id=uuid4()))


class TestSerializeRunRowRedaction:
    def teardown_method(self) -> None:
        set_agent_scope(None)

    def test_enforce_scope_withholds_aggregates(self):
        _enforce_scope()
        out = _serialize_run_row(_row())
        assert out["input_count"] is None
        assert out["cost_estimated_cents"] is None
        assert out["cost_actual_cents"] is None
        # Non-aggregate fields stay intact.
        assert out["status"] == "succeeded"

    def test_no_scope_keeps_aggregates(self):
        set_agent_scope(None)
        out = _serialize_run_row(_row())
        assert out["input_count"] == 120
        assert out["cost_estimated_cents"] == 42
        assert out["cost_actual_cents"] == 40

    def test_shadow_scope_keeps_aggregates(self):
        """Enforcement ramp invariant: shadow observes no change."""
        set_agent_scope(
            AgentScope(agent_id=uuid4(), enforcement_mode="shadow", workspace_id=uuid4())
        )
        out = _serialize_run_row(_row())
        assert out["input_count"] == 120
        assert out["cost_actual_cents"] == 40


class TestCapMessageRedaction:
    def test_redacted_cap_error_omits_true_count(self, monkeypatch):
        import config.settings as settings_mod

        monkeypatch.setattr(
            settings_mod,
            "get_settings",
            lambda: SimpleNamespace(analysis_max_memory_count=10),
        )
        with pytest.raises(ValidationError) as exc_info:
            assert_run_size_within_cap(987654, redact_count=True)
        msg = str(exc_info.value)
        assert "987654" not in msg
        assert "10" in msg  # the cap itself stays named (deployment config)
        details = getattr(exc_info.value, "details", {}) or {}
        assert details.get("memory_count") is None

    def test_default_cap_error_names_count(self, monkeypatch):
        import config.settings as settings_mod

        monkeypatch.setattr(
            settings_mod,
            "get_settings",
            lambda: SimpleNamespace(analysis_max_memory_count=10),
        )
        with pytest.raises(ValidationError) as exc_info:
            assert_run_size_within_cap(987654)
        assert "987654" in str(exc_info.value)
