"""The degraded signal reaches the API and MCP surfaces (#1515).

``SearchService`` detecting the degradation is only half the job — a flag that
never leaves the service is not a signal. These tests pin the two carriers:

- ``RecallResponse.degraded`` / ``.degraded_reason``, which stay absent on the
  happy path because both recall routes serialize with
  ``response_model_exclude_none=True``
- the MCP envelope, which is hand-built and therefore does NOT inherit new
  response fields automatically
"""

import json
from unittest.mock import MagicMock

import pytest

from models.schemas import RecallResponse


class TestResponseSchema:
    def test_absent_on_the_happy_path(self):
        # exclude_none is what both routes use; nothing new appears for
        # existing clients when the search ran normally.
        payload = RecallResponse(results=[]).model_dump(exclude_none=True)
        assert "degraded" not in payload
        assert "degraded_reason" not in payload

    def test_present_and_typed_when_degraded(self):
        payload = RecallResponse(
            results=[], degraded=True, degraded_reason="embedding_unavailable"
        ).model_dump(exclude_none=True)
        assert payload["degraded"] is True
        assert payload["degraded_reason"] == "embedding_unavailable"

    def test_selection_evidence_stays_excluded(self):
        # Guard against the new fields being added in a way that disturbs the
        # #1306 boundary: selection_evidence must never serialize.
        payload = RecallResponse(
            results=[], degraded=True, selection_evidence={"x": 1}
        ).model_dump()
        assert "selection_evidence" not in payload


class TestMcpEnvelope:
    """The MCP handler re-projects fields by hand, so the copy is explicit."""

    @staticmethod
    def _envelope_for(result: RecallResponse) -> dict:
        # Mirror the handler's copy step rather than standing up the whole MCP
        # dispatcher: this asserts the projection contract, which is the part
        # that silently breaks when a response field is added.
        response_data: dict = {"status": "success", "results": [], "count": 0}
        if result.tag_suggestions:
            response_data["tag_suggestions"] = result.tag_suggestions
        if result.degraded:
            response_data["degraded"] = True
            response_data["degraded_reason"] = result.degraded_reason
        return response_data

    def test_degraded_recall_is_marked_in_the_envelope(self):
        env = self._envelope_for(
            RecallResponse(results=[], degraded=True, degraded_reason="embedding_unavailable")
        )
        assert env["degraded"] is True
        assert env["degraded_reason"] == "embedding_unavailable"
        json.dumps(env)  # the handler serializes this; must stay JSON-safe

    def test_healthy_recall_envelope_is_unchanged(self):
        env = self._envelope_for(RecallResponse(results=[]))
        assert "degraded" not in env

    def test_handler_source_copies_the_flag(self):
        # The projection above is a mirror; this pins that the real handler
        # actually performs the copy, so the mirror cannot drift into fiction.
        import inspect

        from mcp_server.tools import memory as mcp_memory

        src = inspect.getsource(mcp_memory.handle_recall)
        assert 'response_data["degraded"]' in src, (
            "handle_recall must copy RecallResponse.degraded into its "
            "hand-built envelope — MCP clients do not inherit new response "
            "fields automatically (#1515)."
        )


@pytest.mark.asyncio
class TestEmptyResultsStillReportDegradation:
    async def test_zero_hits_from_a_degraded_search_are_flagged(self):
        """An empty degraded recall must not read as 'nothing is stored'."""
        from services.memory_service import MemoryService

        service = MemoryService(db=MagicMock())
        request = MagicMock()
        request.include_explore_hints = False

        resp = await service._empty_recall_response(
            request=request,
            selection_config=None,
            search_config=None,
            context_id=MagicMock(),
            degradation={"degraded": True, "reason": "embedding_unavailable"},
        )
        assert resp.degraded is True
        assert resp.degraded_reason == "embedding_unavailable"

    async def test_a_genuinely_empty_recall_is_not_flagged(self):
        from services.memory_service import MemoryService

        service = MemoryService(db=MagicMock())
        request = MagicMock()
        request.include_explore_hints = False

        resp = await service._empty_recall_response(
            request=request,
            selection_config=None,
            search_config=None,
            context_id=MagicMock(),
        )
        assert resp.degraded is None
