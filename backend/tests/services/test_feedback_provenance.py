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
            uuid4(), uuid4(), helpful=True, user_id="cockpit", verdict="check_exit=0"
        )
        row = db.add.call_args.args[0]
        assert row.provenance == FEEDBACK_PROVENANCE_HOST
        assert row.note is not None and "check_exit=0" in row.note

    async def test_host_verdict_is_flattened_and_capped(self):
        # The verdict is the audit trail of the host seam: newlines are flattened
        # (no fractured audit log) and the "host-verdict: " prefix always survives
        # record_feedback's NOTE_MAX_LEN truncation.
        svc, db = _svc_with_existing_memory()
        long_verdict = "line1\nline2\t" + "x" * 5000
        await svc.record_host_feedback(
            uuid4(), uuid4(), helpful=True, user_id="cockpit", verdict=long_verdict
        )
        row = db.add.call_args.args[0]
        assert row.note.startswith("host-verdict: ")
        assert "\n" not in row.note and "\t" not in row.note
        assert len(row.note) <= NOTE_MAX_LEN

    async def test_record_feedback_rejects_forged_provenance(self):
        # Defence in depth: even a service-layer caller cannot inject a bad value
        # (the DB CHECK is the backstop; this fails fast before the insert).
        svc, db = _svc_with_existing_memory()
        with pytest.raises(ValueError, match="provenance"):
            await svc.record_feedback(
                uuid4(), uuid4(), helpful=True, user_id="u", provenance="forged"
            )
        db.add.assert_not_called()
