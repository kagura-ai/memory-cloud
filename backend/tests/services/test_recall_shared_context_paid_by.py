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

import contextlib
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


@contextlib.contextmanager
def _patch_byok_gate(
    *,
    has_key: bool = True,
    provider: str = "openai",
    embedding_model: str = "text-embedding-3-small",
):
    """Patch BOTH EmbeddingService AND ContextSearchConfigRepository.

    The H1 gate in ``MemoryService.recall`` (#708 F4 / loop 2) loads the
    context's ``ContextSearchConfig`` to derive the per-context embedding
    model + provider before constructing ``EmbeddingService``. Tests that
    exercise the gate must mock both dependencies in lock-step.

    Yields the ``has_byok_key`` AsyncMock so tests can assert call args.
    """
    with (
        patch("services.memory_service.EmbeddingService") as embed_cls,
        patch("repositories.config_repository.ContextSearchConfigRepository") as repo_cls,
    ):
        embed_cls.return_value.has_byok_key = AsyncMock(return_value=has_key)
        embed_cls.return_value.provider = provider
        config_mock = MagicMock()
        config_mock.embedding_model = embedding_model
        repo_cls.return_value.create_or_get = AsyncMock(return_value=config_mock)
        yield embed_cls.return_value.has_byok_key


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

    with _patch_byok_gate(has_key=True):
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

    with _patch_byok_gate(has_key=True) as has_byok_mock:
        await svc.recall(
            request=RecallRequest(query="test", k=5),
            user_id="caller_user",
            current_context_id=context_id,
            current_workspace_id=caller_ws,
            context_workspace_id=source_ws,
        )

    # #708 F3: probe must include context_id so the priority of
    # ``_get_user_api_key`` is mirrored — a workspace whose ONLY BYOK
    # row is scoped to a different context must NOT pass the gate.
    has_byok_mock.assert_called_once_with(str(source_ws), context_id=str(context_id))


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

    with _patch_byok_gate(has_key=False):
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

    with _patch_byok_gate(has_key=False):
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


@pytest.mark.asyncio
async def test_shared_context_keyword_mode_skips_byok_gate():
    """#708 F1: keyword-mode search does NOT call embed_with_usage.

    A BM25-only search produces no embedding cost, so the H1 BYOK gate
    must NOT deny it even when the source workspace has no BYOK key.
    Otherwise legitimate keyword-mode shared reads silently 404.
    """
    svc, _db = _make_service()
    caller_ws = uuid4()
    source_ws = uuid4()
    context_id = uuid4()

    with _patch_byok_gate(has_key=False, provider="openai") as has_byok_mock:
        await svc.recall(
            request=RecallRequest(query="test", k=5, search_mode="keyword"),
            user_id="caller_user",
            current_context_id=context_id,
            current_workspace_id=caller_ws,
            context_workspace_id=source_ws,
        )

    has_byok_mock.assert_not_called()
    svc.search_service.hybrid_search.assert_called_once()


@pytest.mark.asyncio
async def test_shared_context_self_hosted_provider_skips_byok_gate():
    """#708 F1: self-hosted provider is free/local — no platform-key fallback path exists.

    ``has_byok_key`` returns False for self-hosted by design (it's not a paid
    provider). The H1 gate must NOT treat that as a denial signal —
    self-hosted-backed shared reads cost nothing to the source workspace.
    """
    svc, _db = _make_service()
    caller_ws = uuid4()
    source_ws = uuid4()
    context_id = uuid4()

    with _patch_byok_gate(has_key=False, provider="self_hosted") as has_byok_mock:
        await svc.recall(
            request=RecallRequest(query="test", k=5),
            user_id="caller_user",
            current_context_id=context_id,
            current_workspace_id=caller_ws,
            context_workspace_id=source_ws,
        )

    has_byok_mock.assert_not_called()
    svc.search_service.hybrid_search.assert_called_once()


@pytest.mark.asyncio
async def test_shared_context_recall_propagates_is_shared_context_read():
    """#708 F2: SearchService must learn this is a cross-workspace Option A read.

    Without the kwarg, ``SearchService.hybrid_search`` treats the request
    as same-workspace, runs ``is_workspace_member(workspace_id=source)``
    (which fails for the cross-workspace caller), and applies the
    ``user_id == caller`` filter (which drops source-workspace memories
    authored by other users — the main Option A scenario returns empty).
    """
    svc, _db = _make_service()
    caller_ws = uuid4()
    source_ws = uuid4()
    context_id = uuid4()

    with _patch_byok_gate(has_key=True, provider="openai"):
        await svc.recall(
            request=RecallRequest(query="test", k=5),
            user_id="caller_user",
            current_context_id=context_id,
            current_workspace_id=caller_ws,
            context_workspace_id=source_ws,
        )

    call_kwargs = svc.search_service.hybrid_search.call_args.kwargs
    assert call_kwargs.get("is_shared_context_read") is True


@pytest.mark.asyncio
async def test_self_workspace_recall_does_not_propagate_is_shared_context_read():
    """#708 F2 fence: same-workspace path must NOT set is_shared_context_read.

    Setting it spuriously would bypass the legitimate is_workspace_member
    check for same-workspace shared contexts where the caller IS the
    workspace they're reading from (no need to skip the check).
    """
    svc, _db = _make_service()
    workspace_id = uuid4()
    context_id = uuid4()

    await svc.recall(
        request=RecallRequest(query="test", k=5),
        user_id="test_user",
        current_context_id=context_id,
        current_workspace_id=workspace_id,
        context_workspace_id=workspace_id,
    )

    call_kwargs = svc.search_service.hybrid_search.call_args.kwargs
    assert call_kwargs.get("is_shared_context_read") is False


@pytest.mark.asyncio
async def test_shared_context_byok_gate_uses_per_context_embedding_model():
    """#708 F4 (Copilot loop 2): gate must use context's configured model.

    The H1 BYOK probe constructs ``EmbeddingService`` from the context's
    ``ContextSearchConfig.embedding_model``, not the platform default. A
    context configured with one provider (e.g. Voyage) running on a
    platform whose default is OpenAI would otherwise be denied
    incorrectly, because ``has_byok_key`` would probe for OpenAI keys.
    """
    svc, _db = _make_service()
    caller_ws = uuid4()
    source_ws = uuid4()
    context_id = uuid4()

    with (
        patch("services.memory_service.EmbeddingService") as embed_cls,
        patch("repositories.config_repository.ContextSearchConfigRepository") as repo_cls,
    ):
        embed_cls.return_value.has_byok_key = AsyncMock(return_value=True)
        embed_cls.return_value.provider = "openai"  # derived from model
        config_mock = MagicMock()
        config_mock.embedding_model = "voyage-3-large"
        repo_cls.return_value.create_or_get = AsyncMock(return_value=config_mock)

        await svc.recall(
            request=RecallRequest(query="test", k=5),
            user_id="caller_user",
            current_context_id=context_id,
            current_workspace_id=caller_ws,
            context_workspace_id=source_ws,
        )

    # EmbeddingService MUST be constructed with the context's model, not the default.
    embed_cls.assert_called_once_with(svc.db, model="voyage-3-large")


@pytest.mark.asyncio
async def test_cross_context_recall_rejects_mixed_privacy():
    """#708 loop 6 (Copilot): cross-context recall MUST reject mixed privacy.

    SearchService derives one ``is_shared_context`` value from the
    primary context and applies it to every context's Qdrant filter.
    Mixed privacy in the list would either drop the user_id filter
    for a private secondary (leaking other users' memories) or keep
    it for a shared primary (returning empty for legitimate readers).
    Hard-reject at the API boundary mirrors the H3 (workspace_mismatch)
    + same-embedding-model invariant pattern.
    """
    import json

    from mcp_server.tools.memory import handle_recall

    workspace_id = uuid4()
    shared_ctx_id = uuid4()
    private_ctx_id = uuid4()

    # Two contexts, same workspace, MIXED privacy.
    shared_ctx = MagicMock()
    shared_ctx.id = shared_ctx_id
    shared_ctx.workspace_id = workspace_id
    shared_ctx.is_private = False
    shared_ctx.name = "shared"
    shared_ctx.display_name = "Shared"
    shared_ctx.is_locked = False
    private_ctx = MagicMock()
    private_ctx.id = private_ctx_id
    private_ctx.workspace_id = workspace_id
    private_ctx.is_private = True
    private_ctx.name = "private"
    private_ctx.display_name = "Private"
    private_ctx.is_locked = False

    resolve_mock = AsyncMock(side_effect=[shared_ctx, private_ctx])

    async def _fake_get_db():
        yield AsyncMock()

    # ``get_db`` and ``_resolve_context_for_read`` are lazy-imported inside
    # ``handle_recall``; patching at the source module is required.
    with (
        patch("db.base.get_db", _fake_get_db),
        patch(
            "mcp_server.tools._helpers._resolve_context_for_read",
            resolve_mock,
        ),
        patch("mcp_server.tools.memory._resolve_context_for_read", resolve_mock),
    ):
        result = await handle_recall(
            args={
                "query": "test",
                "context_ids": [str(shared_ctx_id), str(private_ctx_id)],
                "k": 5,
            },
            user_id="caller_user",
            workspace_id=workspace_id,
        )

    # Response is a list with one TextContent — parse it.
    assert len(result) == 1
    body = json.loads(result[0].text)
    assert body["status"] == "error"
    assert body["error"] == "context_privacy_mismatch"
    assert "privacy" in body["message"].lower()


@pytest.mark.asyncio
async def test_shared_context_byok_gate_deferred_past_empty_cluster_short_circuit():
    """#708 F5 (Copilot loop 2): empty cluster → no embedding call → no gate.

    When ``analysis_cluster`` filter pre-resolves to an empty/unknown
    cluster, ``MemoryService.recall`` returns ``results=[]`` immediately
    without calling ``hybrid_search`` or generating any embedding. The
    H1 gate must not fire on this path — otherwise a no-cost filtered
    read becomes a misleading ``context_not_found`` error.
    """
    svc, _db = _make_service()
    caller_ws = uuid4()
    source_ws = uuid4()
    context_id = uuid4()
    run_id = uuid4()

    with (
        _patch_byok_gate(has_key=False) as has_byok_mock,
        patch(
            "services.analysis.query_service.get_memory_ids_in_cluster",
            AsyncMock(return_value=[]),  # empty cluster — short-circuit fires
        ),
    ):
        response = await svc.recall(
            request=RecallRequest(
                query="test",
                k=5,
                filters={"analysis_cluster": {"run_id": str(run_id), "cluster_index": 0}},
            ),
            user_id="caller_user",
            current_context_id=context_id,
            current_workspace_id=caller_ws,
            context_workspace_id=source_ws,
        )

    assert response.results == []
    # No embedding cost was charged → gate must NOT have fired.
    has_byok_mock.assert_not_called()
    svc.search_service.hybrid_search.assert_not_called()
