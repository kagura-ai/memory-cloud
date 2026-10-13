"""Unit tests for the #1065 forge-resistant feedback provenance stamping.

DB-free: the memory-exists check and the insert are mocked, so these pin the
server-side stamping logic — the agent-callable path can only produce 'agent',
only the host seam stamps 'host', and a forged provenance is rejected. The DB
filtering (host_only aggregation) is pinned end-to-end in
tests/integration/test_retrieval_feedback.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from models.auth import AuditLog
from models.retrieval_feedback import (
    FEEDBACK_PROVENANCE_AGENT,
    FEEDBACK_PROVENANCE_HOST,
    NOTE_MAX_LEN,
)
from services.feedback_service import FeedbackService


def _svc_with_existing_memory():
    """A FeedbackService whose memory-exists guard passes and whose insert is a
    no-op, so we can inspect the row handed to ``db.add``."""
    db = MagicMock()
    exec_result = MagicMock()
    exec_result.scalar_one_or_none = MagicMock(return_value=uuid4())
    db.execute = AsyncMock(return_value=exec_result)
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return FeedbackService(db), db


class TestFeedbackProvenance:
    async def test_record_feedback_defaults_to_agent(self):
        # The public REST/MCP path calls record_feedback without provenance.
        svc, db = _svc_with_existing_memory()
        await svc.record_feedback(uuid4(), uuid4(), helpful=True, user_id="u")
        row = db.add.call_args.args[0]
        assert row.provenance == FEEDBACK_PROVENANCE_AGENT

    async def test_record_host_feedback_stamps_host_and_keeps_verdict(self):
        svc, db = _svc_with_existing_memory()
        await svc.record_host_feedback(
            uuid4(),
            uuid4(),
            helpful=True,
            user_id="cockpit",
            actor_email="cockpit@example.com",
            verdict_source="objective_check",
            verdict_reference="check://pytest/bootstrap-07?exit=0",
            experiment_id="bootstrap-ab-07",
        )
        rows = [call.args[0] for call in db.add.call_args_list]
        row = next(row for row in rows if not isinstance(row, AuditLog))
        audit = next(row for row in rows if isinstance(row, AuditLog))
        assert row.provenance == FEEDBACK_PROVENANCE_HOST
        assert row.note is not None and "check://pytest/bootstrap-07?exit=0" in row.note
        assert audit.action == "host_feedback_recorded"
        assert audit.user_email == "cockpit@example.com"
        assert audit.user_metadata["experiment_id"] == "bootstrap-ab-07"
        assert audit.user_metadata["verdict_source"] == "objective_check"
        assert audit.user_metadata["verdict_reference"] == "check://pytest/bootstrap-07?exit=0"
        assert "context_id" in audit.user_metadata
        assert "memory_id" in audit.user_metadata
        db.commit.assert_awaited_once()

    async def test_host_verdict_is_flattened_and_capped(self):
        # The human-readable event copy is flattened and capped; the structured
        # verdict reference remains separately preserved in the audit row.
        svc, db = _svc_with_existing_memory()
        long_note = "line1\nline2\t" + "x" * 5000
        await svc.record_host_feedback(
            uuid4(),
            uuid4(),
            helpful=True,
            user_id="cockpit",
            verdict_source="trusted_host_check",
            verdict_reference="host://runner/17",
            note=long_note,
        )
        row = next(
            call.args[0] for call in db.add.call_args_list if not isinstance(call.args[0], AuditLog)
        )
        assert row.note.startswith("host-verdict[trusted_host_check]: ")
        assert "\n" not in row.note and "\t" not in row.note
        assert len(row.note) <= NOTE_MAX_LEN

    async def test_record_host_feedback_rejects_missing_independent_reference(self):
        svc, db = _svc_with_existing_memory()
        with pytest.raises(ValueError, match="verdict_reference"):
            await svc.record_host_feedback(
                uuid4(),
                uuid4(),
                helpful=True,
                user_id="cockpit",
                verdict_source="objective_check",
                verdict_reference="  ",
            )
        db.add.assert_not_called()

    async def test_record_host_feedback_rejects_agent_self_report_source(self):
        svc, db = _svc_with_existing_memory()
        with pytest.raises(ValueError, match="verdict_source"):
            await svc.record_host_feedback(
                uuid4(),
                uuid4(),
                helpful=True,
                user_id="cockpit",
                verdict_source="agent_self_report",
                verdict_reference="agent said it worked",
            )
        db.add.assert_not_called()

    async def test_record_feedback_rejects_forged_provenance(self):
        # Defence in depth: even a service-layer caller cannot inject a bad value
        # (the DB CHECK is the backstop; this fails fast before the insert).
        svc, db = _svc_with_existing_memory()
        with pytest.raises(ValueError, match="provenance"):
            await svc.record_feedback(
                uuid4(), uuid4(), helpful=True, user_id="u", provenance="forged"
            )
        db.add.assert_not_called()
