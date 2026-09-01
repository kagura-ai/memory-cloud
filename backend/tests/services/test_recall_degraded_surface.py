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
    """The MCP envelope is hand-built, so the projection is exercised directly.

    These call the SAME function the handler calls
    (``_degraded_response_fields``, alongside the existing
    ``_persistence_response_field`` / ``_lint_response_field``), rather than a
    copy of it written in the test — an earlier version of this file mirrored
    the handler's lines, which meant the tests could stay green while the real
    projection was broken.
    """

    def test_degraded_recall_is_marked_in_the_envelope(self):
        from mcp_server.tools._helpers import _degraded_response_fields

        fields = _degraded_response_fields(
            RecallResponse(results=[], degraded=True, degraded_reason="embedding_unavailable")
        )
        assert fields == {"degraded": True, "degraded_reason": "embedding_unavailable"}
        json.dumps(fields)  # the handler serializes this; must stay JSON-safe

    def test_healthy_recall_adds_no_keys(self):
        from mcp_server.tools._helpers import _degraded_response_fields

        assert _degraded_response_fields(RecallResponse(results=[])) == {}

    def test_a_loose_mock_result_does_not_fabricate_a_degraded_flag(self):
        """MCP tests stub `result` as a bare MagicMock, where every attribute is
        truthy. The projection must not emit a non-serializable flag for one."""
        from unittest.mock import MagicMock

        from mcp_server.tools._helpers import _degraded_response_fields

        result = MagicMock()
        result.degraded = None
        assert _degraded_response_fields(result) == {}

    def test_the_handler_uses_the_shared_projection(self):
        # Pins the wiring itself: the handler must call the helper these tests
        # exercise, or they would be testing an unused function.
        import inspect

        from mcp_server.tools import memory as mcp_memory

        src = inspect.getsource(mcp_memory.handle_recall)
        assert "_degraded_response_fields(result)" in src, (
            "handle_recall must render the degraded flag through "
            "_degraded_response_fields — MCP clients do not inherit new "
            "response fields automatically (#1515)."
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


class TestBootstrapEnvelopeAggregate:
    """A degraded recall must reach the bootstrap envelope's top-level flag.

    Review of #1515 found the aggregate computed `degraded` solely from
    `status == "error"`, so an OK-but-impaired recall component left
    `envelope["degraded"]` false — and BOTH transports derive their audit
    outcome from that value (`"partial" if envelope.get("degraded") else
    "success"`, api/routes/agents.py and mcp_server/tools/agent_bootstrap.py),
    so an impaired bootstrap was recorded as a clean one.
    """

    @staticmethod
    def _aggregate(components: dict) -> bool:
        # Mirrors the expression under test only to document it; the assertion
        # that the real code does this lives in the source check below.
        from services.agent_bootstrap_service import STATUS_ERROR

        return any(
            c.get("status") == STATUS_ERROR or c.get("degraded") is True
            for c in components.values()
        )

    def test_an_ok_but_degraded_component_marks_the_envelope(self):
        assert self._aggregate({"recall": {"status": "ok", "degraded": True}}) is True

    def test_a_healthy_envelope_is_still_clean(self):
        assert self._aggregate({"recall": {"status": "ok"}, "pinned": {"status": "ok"}}) is False

    def test_an_errored_component_still_marks_the_envelope(self):
        assert self._aggregate({"pinned": {"status": "error"}}) is True

    def test_the_real_aggregate_considers_nested_degradation(self):
        import inspect

        from services.agent_bootstrap_service import AgentBootstrapService

        src = inspect.getsource(AgentBootstrapService.build_envelope)
        assert 'c.get("degraded") is True' in src, (
            "build_envelope must fold nested component degradation into the "
            "top-level flag — both transports derive their audit outcome from "
            "it (#1515)."
        )
