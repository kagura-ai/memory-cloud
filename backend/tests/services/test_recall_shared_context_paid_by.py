"""Recall Option A: shared-context reads pay against source workspace (#708).

Verifies the four guardrails identified during gate1 design review for
``MemoryService.recall``:

- **Self-workspace read** (``context_workspace_id == current_workspace_id``):
  downstream ``hybrid_search`` receives ``current_workspace_id``. Regression
  fence — no behavior change for the dominant path.
- **Shared-context read** (``context_workspace_id != current_workspace_id``)
  **with source BYOK present**: downstream ``hybrid_search`` and friends
  receive ``context_workspace_id``. Option A "owner's key for owner's door".
- **Shared-context read with source missing BYOK** (#708 H1, OWASP A04):
  service raises ``NotFoundException("Context", ...)`` rather than falling
  back to the uncapped ``OPENAI_API_KEY`` env path. PR #711's spend cap is
  BYOK-only by design.
- **Shared-context read uniform-error shape** (#708 H2, OWASP A01 /
  CWE-639): the raised exception names "Context" and uses the same shape
  ``_resolve_context_for_read`` produces so callers cannot distinguish
  "source missing BYOK" from "context does not exist for you" via the
  response body.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import RecallRequest
from utils.exceptions import NotFoundException


def _make_service():
    """Return a MemoryService instance with mocked dependencies for unit tests."""
    from services.memory_service import MemoryService

    db = AsyncMock()
    db.execute = AsyncMock()
    svc = MemoryService(db)
    svc.search_service = MagicMock()
    svc.search_service.hybrid_search = AsyncMock(return_value=[])
    svc.memory_repo = MagicMock()
    return svc, db


@pytest.mark.asyncio
async def test_self_workspace_recall_uses_caller_workspace_id():
    """Regression: caller_workspace == context_workspace → no override."""
    svc, _db = _make_service()
    workspace_id = uuid4()
    context_id = uuid4()

    await svc.recall(
        request=RecallRequest(query="test", k=5),
        user_id="test_user",
        current_context_id=context_id,
        current_workspace_id=workspace_id,
        # Self-workspace read: caller and context share the same workspace.
        context_workspace_id=workspace_id,
    )

    svc.search_service.hybrid_search.assert_called_once()
    call_kwargs = svc.search_service.hybrid_search.call_args.kwargs
    assert call_kwargs["workspace_id"] == str(workspace_id)


@pytest.mark.asyncio
async def test_self_workspace_recall_with_none_context_workspace():
    """When ``context_workspace_id`` is None, treat as self-workspace read.

    Callers that have not yet adopted the new kwarg pass ``None`` (the
    default). The override must not trigger — the implementation falls
    back to caller's workspace and preserves the original behavior.
    """
    svc, _db = _make_service()
    workspace_id = uuid4()
    context_id = uuid4()

    await svc.recall(
        request=RecallRequest(query="test", k=5),
        user_id="test_user",
        current_context_id=context_id,
        current_workspace_id=workspace_id,
        # context_workspace_id intentionally omitted (=None).
    )

    call_kwargs = svc.search_service.hybrid_search.call_args.kwargs
    assert call_kwargs["workspace_id"] == str(workspace_id)


@pytest.mark.asyncio
async def test_shared_context_recall_routes_to_source_workspace():
    """Shared-context Option A: downstream workspace_id = context owner."""
    svc, _db = _make_service()
    caller_ws = uuid4()
    source_ws = uuid4()
    context_id = uuid4()

    # Patch EmbeddingService so the BYOK probe returns True (source has key).
    with patch("services.memory_service.EmbeddingService") as embed_svc_cls:
        embed_svc_cls.return_value.has_byok_key = AsyncMock(return_value=True)

        await svc.recall(
            request=RecallRequest(query="test", k=5),
            user_id="caller_user",
            current_context_id=context_id,
            current_workspace_id=caller_ws,
            context_workspace_id=source_ws,
        )

    call_kwargs = svc.search_service.hybrid_search.call_args.kwargs
    assert call_kwargs["workspace_id"] == str(source_ws), (
        "Shared-context read MUST route embedding cost to source workspace"
    )


@pytest.mark.asyncio
async def test_shared_context_recall_calls_has_byok_with_source_workspace_id():
    """H1: BYOK probe MUST query source workspace, not caller's."""
    svc, _db = _make_service()
    caller_ws = uuid4()
    source_ws = uuid4()
    context_id = uuid4()

    with patch("services.memory_service.EmbeddingService") as embed_svc_cls:
        has_byok_mock = AsyncMock(return_value=True)
        embed_svc_cls.return_value.has_byok_key = has_byok_mock

        await svc.recall(
            request=RecallRequest(query="test", k=5),
            user_id="caller_user",
            current_context_id=context_id,
            current_workspace_id=caller_ws,
            context_workspace_id=source_ws,
        )

    has_byok_mock.assert_called_once_with(str(source_ws))


@pytest.mark.asyncio
async def test_shared_context_recall_denies_when_source_no_byok():
    """H1 (OWASP A04): source workspace lacks BYOK → NotFoundException.

    Without this guardrail the request would fall through to
    ``OPENAI_API_KEY`` env (platform-paid + uncapped), defeating PR
    #711's drain-attack mitigation.
    """
    svc, _db = _make_service()
    caller_ws = uuid4()
    source_ws = uuid4()
    context_id = uuid4()

    with patch("services.memory_service.EmbeddingService") as embed_svc_cls:
        embed_svc_cls.return_value.has_byok_key = AsyncMock(return_value=False)

        with pytest.raises(NotFoundException) as exc_info:
            await svc.recall(
                request=RecallRequest(query="test", k=5),
                user_id="caller_user",
                current_context_id=context_id,
                current_workspace_id=caller_ws,
                context_workspace_id=source_ws,
            )

    # hybrid_search MUST NOT be called — the deny happens before any
    # external API key resolution.
    svc.search_service.hybrid_search.assert_not_called()
    # H2 (CWE-639): the exception names "Context" (not "BYOK" or
    # "workspace") so the error body is indistinguishable from a regular
    # context_not_found. The exception's resource_id carries the
    # context_id, not the source_workspace_id, so the response body
    # does not surface the foreign workspace UUID.
    assert "Context" in exc_info.value.message
    assert str(source_ws) not in exc_info.value.message


@pytest.mark.asyncio
async def test_shared_context_recall_uniform_error_does_not_leak_source_workspace():
    """H2 (CWE-639 / OWASP A01): error body MUST NOT leak source workspace.

    Stronger fence than the previous test: the NotFoundException details
    dict and message together MUST NOT mention the source workspace
    UUID. Otherwise an attacker probing context_ids can enumerate which
    contexts belong to BYOK-less foreign workspaces.
    """
    svc, _db = _make_service()
    caller_ws = uuid4()
    source_ws = uuid4()
    context_id = uuid4()

    with patch("services.memory_service.EmbeddingService") as embed_svc_cls:
        embed_svc_cls.return_value.has_byok_key = AsyncMock(return_value=False)

        with pytest.raises(NotFoundException) as exc_info:
            await svc.recall(
                request=RecallRequest(query="test", k=5),
                user_id="caller_user",
                current_context_id=context_id,
                current_workspace_id=caller_ws,
                context_workspace_id=source_ws,
            )

    leaked = str(source_ws)
    assert leaked not in exc_info.value.message
    assert leaked not in str(exc_info.value.details)


@pytest.mark.asyncio
async def test_self_workspace_recall_skips_byok_probe():
    """Optimization fence: same-workspace path must not pay the extra SELECT.

    The has_byok_key probe is cheap (a single indexed SELECT) but it is
    completely unnecessary when the override is a no-op. This test
    ensures we do not regress into calling it on the dominant path.
    """
    svc, _db = _make_service()
    workspace_id = uuid4()
    context_id = uuid4()

    with patch("services.memory_service.EmbeddingService") as embed_svc_cls:
        has_byok_mock = AsyncMock(return_value=True)
        embed_svc_cls.return_value.has_byok_key = has_byok_mock

        await svc.recall(
            request=RecallRequest(query="test", k=5),
            user_id="test_user",
            current_context_id=context_id,
            current_workspace_id=workspace_id,
            context_workspace_id=workspace_id,  # Same as caller
        )

    has_byok_mock.assert_not_called()
