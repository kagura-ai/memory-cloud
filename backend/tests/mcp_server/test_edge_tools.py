"""Tests for MCP edge CRUD tool handlers.

Issue #458: handle_update_edge response payload must reflect post-update DB
state (weight, last_updated, edge_type) rather than the cached pre-update
snapshot loaded by the prior get_edge call. The fix lives at the repository
layer (NeuralEdgeRepository.create_or_update_edge refreshes the returned
ORM after RETURNING), so this handler-level test pins only the contract
the MCP tool exposes: whatever ORM the repo returns is what the response
serializes.

Issue #738: handle_create_edge must pin origin='declared' and default
weight=1.0 so user-asserted edges via MCP `create_edge` are exempt from
`DecayManager` (which only touches `origin='hebbian'`) — see PR #735
follow-up.
"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools._definitions import get_tool_definitions
from mcp_server.tools.edge import handle_create_edge, handle_update_edge
from models.memory import EDGE_ORIGIN_DECLARED

# #1416: the supersede self-heal moved into the shared edge-write service
# (`create_declared_edge` is used by both the MCP tool and the REST
# POST /graph/edges endpoint). The unit tests below exercise the helper at its
# canonical home and patch its logger there.
from services.edge_service import (
    accept_supersede_candidate_if_matching as _accept_supersede_candidate_if_matching,
)


def _mock_edge(src_id, dst_id, *, edge_type="neural_association", weight=0.5, origin="hebbian"):
    """Build a minimal NeuralMemoryEdge-shaped mock for response serialization."""
    e = MagicMock()
    e.src_id = src_id
    e.dst_id = dst_id
    e.edge_type = edge_type
    e.weight = weight
    e.confidence = 1.0
    e.origin = origin
    e.created_at = datetime(2026, 4, 26, 9, 0, 0, tzinfo=UTC)
    e.last_updated = datetime(2026, 4, 26, 9, 0, 0, tzinfo=UTC)
    return e


async def _run_create_edge(args, mock_repo, user_id, workspace_id, context_id):
    """Invoke handle_create_edge with the standard mock environment.

    Shared by TestCreateEdgeDuplicateSemantics (#1321) — the per-test setup
    differs only in `args` and the repo mock's get_edge/create_or_update_edge
    return values, so the patching boilerplate lives here once.
    """
    mock_db = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.rollback = AsyncMock()

    async def mock_get_db():
        yield mock_db

    mock_ctx = MagicMock()
    mock_ctx.id = context_id
    mock_ctx.workspace_id = workspace_id

    with (
        patch("db.base.get_db", new=mock_get_db),
        patch(
            "repositories.neural_edge.NeuralEdgeRepository",
            return_value=mock_repo,
        ),
        patch(
            "mcp_server.tools.edge._resolve_context",
            new_callable=AsyncMock,
            return_value=mock_ctx,
        ),
        patch(
            "mcp_server.tools.edge._check_viewer_permission",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "mcp_server.tools.edge._log_tool_usage",
            new_callable=AsyncMock,
        ),
    ):
        result = await handle_create_edge(
            {"context_id": str(context_id), **args},
            user_id,
            workspace_id,
        )
    return json.loads(result[0].text)


class TestUpdateEdgeResponsePayload:
    """Issue #458 regression: response must serialize whatever the repo returns."""

    @pytest.fixture
    def user_id(self):
        return "test_user_458"

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.fixture
    def context_id(self):
        return uuid4()

    @pytest.mark.asyncio
    async def test_response_reflects_repo_returned_edge(self, user_id, workspace_id, context_id):
        """The response payload mirrors the post-update edge returned by the repo.

        The fix lives in NeuralEdgeRepository.create_or_update_edge (refreshes
        the ORM after RETURNING). Here we pin the handler contract: response
        weight/last_updated equal whatever the repo returned, with no
        intervening transformation that could re-stale the value.
        """
        src_id = uuid4()
        dst_id = uuid4()
        existing = _mock_edge(src_id, dst_id, edge_type="neural_association", weight=0.5)
        post_update = _mock_edge(src_id, dst_id, edge_type="neural_association", weight=0.8)
        post_update.last_updated = datetime(2026, 4, 26, 9, 19, 11, tzinfo=UTC)

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        mock_repo = MagicMock()
        mock_repo.get_edge = AsyncMock(return_value=existing)
        mock_repo.create_or_update_edge = AsyncMock(return_value=post_update)

        mock_ctx = MagicMock()
        mock_ctx.id = context_id
        mock_ctx.workspace_id = workspace_id

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
            patch(
                "mcp_server.tools.edge._resolve_context",
                new_callable=AsyncMock,
                return_value=mock_ctx,
            ),
            patch(
                "mcp_server.tools.edge._check_viewer_permission",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "mcp_server.tools.edge._log_tool_usage",
                new_callable=AsyncMock,
            ),
        ):
            result = await handle_update_edge(
                {
                    "source_id": str(src_id),
                    "target_id": str(dst_id),
                    "weight": 0.8,
                    "context_id": str(context_id),
                },
                user_id,
                workspace_id,
            )

        data = json.loads(result[0].text)
        assert data["status"] == "success"
        assert data["edge"]["weight"] == 0.8
        assert data["edge"]["last_updated"] == "2026-04-26T09:19:11+00:00"
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_error_when_edge_missing(self, user_id, workspace_id, context_id):
        """update_edge for a nonexistent edge returns edge_not_found without upsert."""
        src_id = uuid4()
        dst_id = uuid4()

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        mock_repo = MagicMock()
        mock_repo.get_edge = AsyncMock(return_value=None)
        mock_repo.create_or_update_edge = AsyncMock()

        mock_ctx = MagicMock()
        mock_ctx.id = context_id
        mock_ctx.workspace_id = workspace_id

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
            patch(
                "mcp_server.tools.edge._resolve_context",
                new_callable=AsyncMock,
                return_value=mock_ctx,
            ),
            patch(
                "mcp_server.tools.edge._check_viewer_permission",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "mcp_server.tools.edge._log_tool_usage",
                new_callable=AsyncMock,
            ),
        ):
            result = await handle_update_edge(
                {
                    "source_id": str(src_id),
                    "target_id": str(dst_id),
                    "weight": 0.8,
                    "context_id": str(context_id),
                },
                user_id,
                workspace_id,
            )

        data = json.loads(result[0].text)
        assert data["status"] == "error"
        assert data["error"] == "edge_not_found"
        mock_repo.create_or_update_edge.assert_not_awaited()


class TestCreateEdgeOriginAndWeight:
    """Issue #738: MCP `create_edge` is the user-assertion path — rows MUST be
    written with `origin='declared'` so they survive the nightly Hebbian
    decay loop, and the default weight MUST be 1.0 (full confidence)
    rather than the prior 0.5 carryover. PR #735 documented this contract
    in concepts.md / architecture.md; this test trio pins it at both the
    handler layer and the MCP tool-definition schema layer (#814).
    """

    @pytest.fixture
    def user_id(self):
        return "test_user_738"

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.fixture
    def context_id(self):
        return uuid4()

    @pytest.mark.asyncio
    async def test_writes_declared_origin(self, user_id, workspace_id, context_id):
        """handle_create_edge MUST pass origin='declared' to the repository."""
        src_id = uuid4()
        dst_id = uuid4()
        created = _mock_edge(src_id, dst_id, edge_type="related_to", weight=1.0, origin="declared")

        mock_repo = MagicMock()
        mock_repo.get_edge = AsyncMock(return_value=None)
        mock_repo.create_or_update_edge = AsyncMock(return_value=created)

        data = await _run_create_edge(
            {"source_id": str(src_id), "target_id": str(dst_id)},
            mock_repo,
            user_id,
            workspace_id,
            context_id,
        )

        assert data["status"] == "success"
        mock_repo.create_or_update_edge.assert_awaited_once()
        kwargs = mock_repo.create_or_update_edge.await_args.kwargs
        assert kwargs["origin"] == EDGE_ORIGIN_DECLARED
        assert kwargs["origin"] == "declared"  # pin the literal as a drift guard

    @pytest.mark.asyncio
    async def test_default_weight_is_1(self, user_id, workspace_id, context_id):
        """When `weight` is omitted from args, handler MUST default to 1.0."""
        src_id = uuid4()
        dst_id = uuid4()
        created = _mock_edge(src_id, dst_id, edge_type="related_to", weight=1.0, origin="declared")

        mock_repo = MagicMock()
        mock_repo.get_edge = AsyncMock(return_value=None)
        mock_repo.create_or_update_edge = AsyncMock(return_value=created)

        data = await _run_create_edge(
            # weight intentionally omitted
            {"source_id": str(src_id), "target_id": str(dst_id)},
            mock_repo,
            user_id,
            workspace_id,
            context_id,
        )

        assert data["status"] == "success"
        mock_repo.create_or_update_edge.assert_awaited_once()
        kwargs = mock_repo.create_or_update_edge.await_args.kwargs
        assert kwargs["weight"] == 1.0

    def test_schema_default_matches_handler_default(self):
        """Issue #814: MCP tool definition's `create_edge.weight.default` MUST equal
        the handler's runtime default (1.0). Pinning both sides forces any future
        change to the handler to update the tool definition in the same PR, which
        prevents the docs/runtime drift that Copilot caught on PR #812 and surfaced
        as #814. The handler-side pin is `test_default_weight_is_1` above; this is
        its schema-side counterpart.
        """
        create_edge = next(t for t in get_tool_definitions() if t["name"] == "create_edge")
        weight_schema = create_edge["inputSchema"]["properties"]["weight"]
        assert weight_schema["default"] == 1.0


class TestUpdateEdgeWeightNoDefault:
    """Issue #816: update_edge has NO weight default. Omitting weight preserves
    the edge's current value, the schema declares no default, and the guarded
    `_parse_float` call must not carry the unreachable 0.5 fallback that drifted
    from create_edge's 1.0 (#814)."""

    @pytest.fixture
    def user_id(self):
        return "test_user_816"

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.fixture
    def context_id(self):
        return uuid4()

    @pytest.mark.asyncio
    async def test_omitted_weight_preserves_current(self, user_id, workspace_id, context_id):
        """edge_type-only update (weight omitted) leaves the existing weight intact.

        Pins the documented contract: an omitted ``weight`` is NOT reset to any
        handler default — the repo is called with the existing edge's weight, so
        a type-only update never silently rewrites weight.
        """
        src_id = uuid4()
        dst_id = uuid4()
        existing = _mock_edge(src_id, dst_id, edge_type="neural_association", weight=0.5)
        post_update = _mock_edge(src_id, dst_id, edge_type="related_to", weight=0.5)

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        mock_repo = MagicMock()
        mock_repo.get_edge = AsyncMock(return_value=existing)
        mock_repo.create_or_update_edge = AsyncMock(return_value=post_update)

        mock_ctx = MagicMock()
        mock_ctx.id = context_id
        mock_ctx.workspace_id = workspace_id

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
            patch(
                "mcp_server.tools.edge._resolve_context",
                new_callable=AsyncMock,
                return_value=mock_ctx,
            ),
            patch(
                "mcp_server.tools.edge._check_viewer_permission",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "mcp_server.tools.edge._log_tool_usage",
                new_callable=AsyncMock,
            ),
        ):
            result = await handle_update_edge(
                {
                    "source_id": str(src_id),
                    "target_id": str(dst_id),
                    "edge_type": "related_to",
                    # weight intentionally omitted
                    "context_id": str(context_id),
                },
                user_id,
                workspace_id,
            )

        data = json.loads(result[0].text)
        assert data["status"] == "success"
        # Repo MUST be called with the EXISTING weight (preserved), not a default.
        kwargs = mock_repo.create_or_update_edge.await_args.kwargs
        assert kwargs["weight"] == 0.5
        assert kwargs["edge_type"] == "related_to"

    def test_schema_has_no_weight_default(self):
        """update_edge.weight has NO schema default — the load-bearing fact behind
        "omitting weight preserves current value". Contrast create_edge.weight,
        which pins default=1.0 (test_schema_default_matches_handler_default)."""
        update_edge = next(t for t in get_tool_definitions() if t["name"] == "update_edge")
        weight_schema = update_edge["inputSchema"]["properties"]["weight"]
        assert "default" not in weight_schema

    def test_parse_float_no_fallback_when_default_omitted(self):
        """`_parse_float` with no ``default`` returns (None, None) on None input.

        This pins the contract that lets the guarded update_edge call site omit a
        fallback (`_parse_float(new_weight_raw, "weight", 0.0, 3.0)`) instead of
        passing the unreachable 0.5. A provided value still parses normally.
        """
        from mcp_server.tools.edge import _parse_float

        assert _parse_float(None, "weight", 0.0, 3.0) == (None, None)
        assert _parse_float(0.8, "weight", 0.0, 3.0) == (0.8, None)


class TestCreateEdgeDuplicateSemantics:
    """Issue #1321: create_edge duplicate behavior is deterministic and declared.

    Contract:
    - no existing edge            → insert, ``operation: "created"``
    - existing origin != declared → upsert, ``operation: "updated"`` + ``previous``
    - existing declared, values identical → no write, ``operation: "unchanged"``
    - existing declared, values differ, no overwrite → error ``edge_exists``
    - existing declared, values differ, overwrite=true → upsert, ``operation: "updated"``
    """

    @pytest.fixture
    def user_id(self):
        return "test_user_1321"

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.fixture
    def context_id(self):
        return uuid4()

    @pytest.mark.asyncio
    async def test_new_edge_returns_operation_created(self, user_id, workspace_id, context_id):
        """No existing edge → insert path reports operation='created'."""
        src_id, dst_id = uuid4(), uuid4()
        created = _mock_edge(src_id, dst_id, edge_type="related_to", weight=1.0, origin="declared")

        mock_repo = MagicMock()
        mock_repo.get_edge = AsyncMock(return_value=None)
        mock_repo.create_or_update_edge = AsyncMock(return_value=created)

        data = await _run_create_edge(
            {"source_id": str(src_id), "target_id": str(dst_id)},
            mock_repo,
            user_id,
            workspace_id,
            context_id,
        )

        assert data["status"] == "success"
        assert data["operation"] == "created"
        assert data["edge"]["origin"] == "declared"
        assert "previous" not in data
        # Race belt-and-suspenders: even the believed-fresh insert arm keeps
        # the declared-type guard, so a declared edge created between the
        # SELECT and the upsert cannot be silently retyped.
        kwargs = mock_repo.create_or_update_edge.await_args.kwargs
        assert kwargs["protect_declared_link"] is True

    @pytest.mark.asyncio
    async def test_existing_hebbian_edge_updates_with_previous(
        self, user_id, workspace_id, context_id
    ):
        """A hebbian (auto-created) edge is upgradeable: upsert proceeds, but the
        response says operation='updated' and carries the pre-image."""
        src_id, dst_id = uuid4(), uuid4()
        existing = _mock_edge(
            src_id, dst_id, edge_type="neural_association", weight=0.4, origin="hebbian"
        )
        post = _mock_edge(src_id, dst_id, edge_type="supersedes", weight=1.0, origin="declared")

        mock_repo = MagicMock()
        mock_repo.get_edge = AsyncMock(return_value=existing)
        mock_repo.create_or_update_edge = AsyncMock(return_value=post)

        data = await _run_create_edge(
            {
                "source_id": str(src_id),
                "target_id": str(dst_id),
                "edge_type": "supersedes",
            },
            mock_repo,
            user_id,
            workspace_id,
            context_id,
        )

        assert data["status"] == "success"
        assert data["operation"] == "updated"
        assert data["previous"] == {
            "edge_type": "neural_association",
            "weight": 0.4,
            "confidence": 1.0,
            "origin": "hebbian",
        }
        assert data["edge"]["edge_type"] == "supersedes"

    @pytest.mark.asyncio
    async def test_declared_edge_identical_values_is_unchanged(
        self, user_id, workspace_id, context_id
    ):
        """Re-asserting a declared edge with identical values is a no-op success —
        client timeout-retries of create_edge stay idempotent instead of erroring."""
        src_id, dst_id = uuid4(), uuid4()
        existing = _mock_edge(
            src_id, dst_id, edge_type="related_to", weight=1.0, origin=EDGE_ORIGIN_DECLARED
        )

        mock_repo = MagicMock()
        mock_repo.get_edge = AsyncMock(return_value=existing)
        mock_repo.create_or_update_edge = AsyncMock()

        data = await _run_create_edge(
            # edge_type/weight/confidence omitted → defaults related_to/1.0/1.0
            # match the existing row exactly.
            {"source_id": str(src_id), "target_id": str(dst_id)},
            mock_repo,
            user_id,
            workspace_id,
            context_id,
        )

        assert data["status"] == "success"
        assert data["operation"] == "unchanged"
        assert data["edge"]["weight"] == 1.0
        assert data["edge"]["origin"] == "declared"
        mock_repo.create_or_update_edge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_declared_edge_conflicting_values_rejected(
        self, user_id, workspace_id, context_id
    ):
        """A declared edge with different values is protected: error edge_exists,
        no write, message points to update_edge / overwrite=true."""
        src_id, dst_id = uuid4(), uuid4()
        existing = _mock_edge(
            src_id, dst_id, edge_type="related_to", weight=1.0, origin=EDGE_ORIGIN_DECLARED
        )

        mock_repo = MagicMock()
        mock_repo.get_edge = AsyncMock(return_value=existing)
        mock_repo.create_or_update_edge = AsyncMock()

        data = await _run_create_edge(
            {"source_id": str(src_id), "target_id": str(dst_id), "weight": 0.9},
            mock_repo,
            user_id,
            workspace_id,
            context_id,
        )

        assert data["status"] == "error"
        assert data["error"] == "edge_exists"
        assert "update_edge" in data["message"]
        assert "overwrite" in data["message"]
        # The pre-existing declared edge is echoed so the caller can decide.
        assert data["existing_edge"]["edge_type"] == "related_to"
        assert data["existing_edge"]["weight"] == 1.0
        mock_repo.create_or_update_edge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_declared_edge_overwrite_flag_updates(self, user_id, workspace_id, context_id):
        """overwrite=true is the explicit opt-in: the declared edge is re-asserted
        (upsert without the declared-type guard) and the pre-image is returned."""
        src_id, dst_id = uuid4(), uuid4()
        existing = _mock_edge(
            src_id, dst_id, edge_type="related_to", weight=1.0, origin=EDGE_ORIGIN_DECLARED
        )
        post = _mock_edge(src_id, dst_id, edge_type="depends_on", weight=0.9, origin="declared")

        mock_repo = MagicMock()
        mock_repo.get_edge = AsyncMock(return_value=existing)
        mock_repo.create_or_update_edge = AsyncMock(return_value=post)

        data = await _run_create_edge(
            {
                "source_id": str(src_id),
                "target_id": str(dst_id),
                "edge_type": "depends_on",
                "weight": 0.9,
                "overwrite": True,
            },
            mock_repo,
            user_id,
            workspace_id,
            context_id,
        )

        assert data["status"] == "success"
        assert data["operation"] == "updated"
        assert data["previous"]["edge_type"] == "related_to"
        assert data["previous"]["weight"] == 1.0
        kwargs = mock_repo.create_or_update_edge.await_args.kwargs
        assert kwargs["protect_declared_link"] is False

    @pytest.mark.asyncio
    async def test_semantic_origin_edge_updates_not_protected(
        self, user_id, workspace_id, context_id
    ):
        """Only origin='declared' is protected — a semantic (sleep-discovered)
        edge is machine provenance, so the assertion applies and the pre-image
        shows origin='semantic'. Pins the predicate as == 'declared', not
        != 'hebbian'."""
        src_id, dst_id = uuid4(), uuid4()
        existing = _mock_edge(src_id, dst_id, edge_type="related_to", weight=0.7, origin="semantic")
        post = _mock_edge(src_id, dst_id, edge_type="related_to", weight=1.0, origin="semantic")

        mock_repo = MagicMock()
        mock_repo.get_edge = AsyncMock(return_value=existing)
        mock_repo.create_or_update_edge = AsyncMock(return_value=post)

        data = await _run_create_edge(
            {"source_id": str(src_id), "target_id": str(dst_id)},
            mock_repo,
            user_id,
            workspace_id,
            context_id,
        )

        assert data["status"] == "success"
        assert data["operation"] == "updated"
        assert data["previous"]["origin"] == "semantic"
        mock_repo.create_or_update_edge.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_overwrite_true_with_no_existing_edge_creates(
        self, user_id, workspace_id, context_id
    ):
        """overwrite=true on a fresh pair is a plain create — and the explicit
        opt-in also disables the race-window declared-type guard."""
        src_id, dst_id = uuid4(), uuid4()
        created = _mock_edge(src_id, dst_id, edge_type="related_to", weight=1.0, origin="declared")

        mock_repo = MagicMock()
        mock_repo.get_edge = AsyncMock(return_value=None)
        mock_repo.create_or_update_edge = AsyncMock(return_value=created)

        data = await _run_create_edge(
            {"source_id": str(src_id), "target_id": str(dst_id), "overwrite": True},
            mock_repo,
            user_id,
            workspace_id,
            context_id,
        )

        assert data["status"] == "success"
        assert data["operation"] == "created"
        assert data["edge"]["origin"] == "declared"
        kwargs = mock_repo.create_or_update_edge.await_args.kwargs
        assert kwargs["protect_declared_link"] is False

    @pytest.mark.asyncio
    async def test_overwrite_non_boolean_rejected(self, user_id, workspace_id, context_id):
        """A junk string for `overwrite` must fail closed (validation_error),
        never count as truthy — bool("null") is True and would silently enable
        the destructive path the flag is guarding."""
        src_id, dst_id = uuid4(), uuid4()

        mock_repo = MagicMock()
        mock_repo.get_edge = AsyncMock()
        mock_repo.create_or_update_edge = AsyncMock()

        data = await _run_create_edge(
            {"source_id": str(src_id), "target_id": str(dst_id), "overwrite": "null"},
            mock_repo,
            user_id,
            workspace_id,
            context_id,
        )

        assert data["status"] == "error"
        assert data["error"] == "validation_error"
        mock_repo.get_edge.assert_not_awaited()
        mock_repo.create_or_update_edge.assert_not_awaited()

    def test_schema_declares_overwrite_flag(self):
        """The tool definition documents the duplicate contract: an `overwrite`
        boolean (default false) and an `operation` field in the response."""
        create_edge = next(t for t in get_tool_definitions() if t["name"] == "create_edge")
        overwrite_schema = create_edge["inputSchema"]["properties"]["overwrite"]
        assert overwrite_schema["type"] == "boolean"
        assert overwrite_schema["default"] is False
        assert "operation" in create_edge["description"]


class TestSupersedeAcceptanceTelemetry:
    """#1403 option B: creating a supersedes edge that confirms a stored
    suggestion records the acceptance and self-heals (clears the suggestion)."""

    @staticmethod
    def _db_returning(memory):
        db = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=memory)
        db.execute = AsyncMock(return_value=result)
        return db

    @pytest.mark.asyncio
    async def test_matching_candidate_accepted_and_cleared(self):
        """When src.supersede_candidate.memory_id == dst, the acceptance event
        fires and the stored candidate column is cleared to None."""
        src_id, dst_id = uuid4(), uuid4()
        memory = MagicMock()
        memory.supersede_candidate = {"memory_id": str(dst_id), "similarity": 0.9}
        db = self._db_returning(memory)

        with patch("services.edge_service.logger") as mock_logger:
            await _accept_supersede_candidate_if_matching(db, src_id=src_id, dst_id=dst_id)

        # Self-heal: the accepted suggestion is cleared.
        assert memory.supersede_candidate is None
        events = {c.args[0]: c.kwargs for c in mock_logger.info.call_args_list if c.args}
        assert "supersede_suggestion_accepted" in events
        assert events["supersede_suggestion_accepted"]["superseded_memory_id"] == str(dst_id)
        assert events["supersede_suggestion_accepted"]["memory_id"] == str(src_id)

    @pytest.mark.asyncio
    async def test_non_matching_candidate_untouched(self):
        """A supersedes edge to a DIFFERENT target than the stored candidate is
        not an acceptance — nothing is cleared and no event fires."""
        src_id, dst_id = uuid4(), uuid4()
        other = uuid4()
        memory = MagicMock()
        candidate = {"memory_id": str(other), "similarity": 0.9}
        memory.supersede_candidate = candidate
        db = self._db_returning(memory)

        with patch("services.edge_service.logger") as mock_logger:
            await _accept_supersede_candidate_if_matching(db, src_id=src_id, dst_id=dst_id)

        assert memory.supersede_candidate == candidate  # untouched
        emitted = [c.args[0] for c in mock_logger.info.call_args_list if c.args]
        assert "supersede_suggestion_accepted" not in emitted

    @pytest.mark.asyncio
    async def test_no_stored_candidate_is_noop(self):
        """No stored candidate → no acceptance event, no mutation."""
        src_id, dst_id = uuid4(), uuid4()
        memory = MagicMock()
        memory.supersede_candidate = None
        db = self._db_returning(memory)

        with patch("services.edge_service.logger") as mock_logger:
            await _accept_supersede_candidate_if_matching(db, src_id=src_id, dst_id=dst_id)

        assert memory.supersede_candidate is None
        emitted = [c.args[0] for c in mock_logger.info.call_args_list if c.args]
        assert "supersede_suggestion_accepted" not in emitted

    @staticmethod
    def _update_edge_env(existing, post_update):
        """Standard fully-mocked environment for a handle_update_edge call."""
        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        mock_repo = MagicMock()
        mock_repo.get_edge = AsyncMock(return_value=existing)
        mock_repo.create_or_update_edge = AsyncMock(return_value=post_update)

        mock_ctx = MagicMock()
        mock_ctx.id = uuid4()
        mock_ctx.workspace_id = uuid4()
        return mock_get_db, mock_repo, mock_ctx

    @pytest.mark.asyncio
    async def test_update_edge_to_supersedes_records_acceptance(self):
        """#1403 F4: retyping an edge to 'supersedes' via update_edge (not only
        create_edge) must confirm/clear a stored suggestion — else a suggestion
        accepted through update_edge keeps resurfacing on recall/reference."""
        src_id, dst_id, context_id, workspace_id = uuid4(), uuid4(), uuid4(), uuid4()
        existing = _mock_edge(src_id, dst_id, edge_type="related_to", weight=0.5)
        post_update = _mock_edge(src_id, dst_id, edge_type="supersedes", weight=0.5)
        mock_get_db, mock_repo, mock_ctx = self._update_edge_env(existing, post_update)

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch("repositories.neural_edge.NeuralEdgeRepository", return_value=mock_repo),
            patch(
                "mcp_server.tools.edge._resolve_context",
                new_callable=AsyncMock,
                return_value=mock_ctx,
            ),
            patch(
                "mcp_server.tools.edge._check_viewer_permission",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("mcp_server.tools.edge._log_tool_usage", new_callable=AsyncMock),
            patch(
                "mcp_server.tools.edge.accept_supersede_candidate_if_matching",
                new_callable=AsyncMock,
            ) as accept,
        ):
            result = await handle_update_edge(
                {
                    "source_id": str(src_id),
                    "target_id": str(dst_id),
                    "edge_type": "supersedes",
                    "context_id": str(context_id),
                },
                "user-f4",
                workspace_id,
            )

        assert json.loads(result[0].text)["status"] == "success"
        accept.assert_awaited_once()
        assert accept.await_args.kwargs["src_id"] == src_id
        assert accept.await_args.kwargs["dst_id"] == dst_id

    @pytest.mark.asyncio
    async def test_update_edge_non_supersedes_skips_acceptance(self):
        """A weight-only update (edge stays non-'supersedes') must not touch the
        supersede-candidate machinery."""
        src_id, dst_id, context_id, workspace_id = uuid4(), uuid4(), uuid4(), uuid4()
        existing = _mock_edge(src_id, dst_id, edge_type="related_to", weight=0.5)
        post_update = _mock_edge(src_id, dst_id, edge_type="related_to", weight=0.8)
        mock_get_db, mock_repo, mock_ctx = self._update_edge_env(existing, post_update)

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch("repositories.neural_edge.NeuralEdgeRepository", return_value=mock_repo),
            patch(
                "mcp_server.tools.edge._resolve_context",
                new_callable=AsyncMock,
                return_value=mock_ctx,
            ),
            patch(
                "mcp_server.tools.edge._check_viewer_permission",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("mcp_server.tools.edge._log_tool_usage", new_callable=AsyncMock),
            patch(
                "mcp_server.tools.edge.accept_supersede_candidate_if_matching",
                new_callable=AsyncMock,
            ) as accept,
        ):
            result = await handle_update_edge(
                {
                    "source_id": str(src_id),
                    "target_id": str(dst_id),
                    "weight": 0.8,
                    "context_id": str(context_id),
                },
                "user-f4",
                workspace_id,
            )

        assert json.loads(result[0].text)["status"] == "success"
        accept.assert_not_awaited()
