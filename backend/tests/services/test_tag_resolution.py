"""Read-time tag resolution against a context's vocabulary (#1503).

The vocabulary query is mocked (it is a plain aggregate over ``memories``); what
is under test is the resolution policy layered on top — what widens a filter,
what only hints, and the fail-open behaviour that keeps a recall alive when the
vocabulary read breaks.
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.tag_resolution import (
    MAX_EXPANSION_PER_TAG,
    MAX_SUGGESTIONS_PER_TAG,
    VOCABULARY_LIMIT,
    expand_tag_filter,
    fetch_vocabulary,
    suggest_tags,
)

WS = uuid4()
CTX = uuid4()
USER = "caller-1"


@pytest.fixture(autouse=True)
def _unshared_context():
    """Default every test to a NON-shared context (the user-scoped path).

    fetch_vocabulary resolves sharing per call; tests that care about the shared
    branch override this with _shared_context().
    """
    with patch("services.context_service.ContextService") as cls:
        cls.return_value.is_context_shared = AsyncMock(return_value=False)
        yield cls


@contextlib.contextmanager
def _shared_context():
    with patch("services.context_service.ContextService") as cls:
        cls.return_value.is_context_shared = AsyncMock(return_value=True)
        yield


def _db_with_vocabulary(vocabulary: dict[str, int]):
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = list(vocabulary.items())
    db.execute = AsyncMock(return_value=result)
    return db


def _broken_db():
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("vocabulary read exploded"))
    return db


class TestExpandTagFilter:
    @pytest.mark.asyncio
    async def test_mechanical_variants_are_added(self):
        db = _db_with_vocabulary({"Dev_Environment": 12, "dev environment": 3, "python": 40})
        expanded, added = await expand_tag_filter(
            db, workspace_id=WS, context_id=CTX, user_id=USER, tags=["dev-environment"]
        )
        assert set(expanded) == {"dev-environment", "Dev_Environment", "dev environment"}
        assert added == {"dev-environment": ["Dev_Environment", "dev environment"]}

    @pytest.mark.asyncio
    async def test_abbreviations_are_not_added(self):
        """The issue's example must NOT silently widen the filter."""
        db = _db_with_vocabulary({"dev-env": 3, "authentication": 9})
        expanded, added = await expand_tag_filter(
            db, workspace_id=WS, context_id=CTX, user_id=USER, tags=["dev-environment", "auth"]
        )
        assert expanded == ["dev-environment", "auth"]
        assert added == {}

    @pytest.mark.asyncio
    async def test_requested_tag_is_kept_even_when_absent_from_the_vocabulary(self):
        """Expansion widens; it must never replace what the caller asked for."""
        db = _db_with_vocabulary({"unrelated": 5})
        expanded, _ = await expand_tag_filter(
            db, workspace_id=WS, context_id=CTX, user_id=USER, tags=["never-used"]
        )
        assert expanded == ["never-used"]

    @pytest.mark.asyncio
    async def test_no_duplicates_when_two_requested_tags_share_variants(self):
        db = _db_with_vocabulary({"Dev_Environment": 2})
        expanded, _ = await expand_tag_filter(
            db,
            workspace_id=WS,
            context_id=CTX,
            user_id=USER,
            tags=["dev-environment", "dev_environment"],
        )
        assert len(expanded) == len(set(expanded))
        assert "Dev_Environment" in expanded

    @pytest.mark.asyncio
    async def test_expansion_is_bounded_per_tag(self):
        """A pathological vocabulary cannot inflate the Qdrant filter."""
        vocabulary = {
            f"dev{'-' * (i + 1)}environment": 1 for i in range(MAX_EXPANSION_PER_TAG + 15)
        }
        db = _db_with_vocabulary(vocabulary)
        _, added = await expand_tag_filter(
            db, workspace_id=WS, context_id=CTX, user_id=USER, tags=["dev-environment"]
        )
        assert len(added["dev-environment"]) <= MAX_EXPANSION_PER_TAG

    @pytest.mark.asyncio
    async def test_punctuation_only_tag_matches_nothing(self):
        """An empty fold must not collide with every other empty fold."""
        db = _db_with_vocabulary({"***": 4, "python": 9})
        expanded, added = await expand_tag_filter(
            db, workspace_id=WS, context_id=CTX, user_id=USER, tags=["---"]
        )
        assert expanded == ["---"]
        assert added == {}

    @pytest.mark.asyncio
    async def test_vocabulary_failure_returns_the_filter_unchanged(self):
        """Widening is an enhancement — it must never break a recall."""
        expanded, added = await expand_tag_filter(
            _broken_db(), workspace_id=WS, context_id=CTX, user_id=USER, tags=["python"]
        )
        assert expanded == ["python"]
        assert added == {}

    @pytest.mark.asyncio
    async def test_query_is_scoped_and_bounded(self):
        """The vocabulary read must be per authorized context and capped."""
        db = _db_with_vocabulary({"python": 1})
        await expand_tag_filter(db, workspace_id=WS, context_id=CTX, user_id=USER, tags=["python"])
        sql = str(db.execute.await_args.args[0].compile(compile_kwargs={"literal_binds": True}))
        assert "workspace_id" in sql and "context_id" in sql
        assert "deleted_at IS NULL" in sql
        assert f"LIMIT {VOCABULARY_LIMIT}" in sql


class TestSuggestTags:
    @pytest.mark.asyncio
    async def test_abbreviation_is_suggested_with_its_count(self):
        db = _db_with_vocabulary({"dev-env": 3, "python": 40})
        assert await suggest_tags(
            db, workspace_id=WS, context_id=CTX, user_id=USER, tags=["dev-environment"]
        ) == {"dev-environment": ["dev-env (3)"]}

    @pytest.mark.asyncio
    async def test_nothing_close_returns_empty(self):
        """Empty is the signal that the topic genuinely is not stored."""
        db = _db_with_vocabulary({"cooking": 5, "travel": 2})
        assert (
            await suggest_tags(
                db, workspace_id=WS, context_id=CTX, user_id=USER, tags=["kubernetes"]
            )
            == {}
        )

    @pytest.mark.asyncio
    async def test_the_requested_tag_never_suggests_itself(self):
        db = _db_with_vocabulary({"python": 40})
        assert (
            await suggest_tags(db, workspace_id=WS, context_id=CTX, user_id=USER, tags=["python"])
            == {}
        )

    @pytest.mark.asyncio
    async def test_suggestions_are_bounded_per_tag(self):
        vocabulary = {f"troubleshooting{i}": i for i in range(MAX_SUGGESTIONS_PER_TAG + 10)}
        db = _db_with_vocabulary(vocabulary)
        out = await suggest_tags(
            db, workspace_id=WS, context_id=CTX, user_id=USER, tags=["troubleshooting"]
        )
        assert len(out["troubleshooting"]) <= MAX_SUGGESTIONS_PER_TAG

    @pytest.mark.asyncio
    async def test_vocabulary_failure_returns_no_suggestions(self):
        assert (
            await suggest_tags(
                _broken_db(), workspace_id=WS, context_id=CTX, user_id=USER, tags=["x"]
            )
            == {}
        )


class TestRecallWiring:
    """The service-side policy: when expansion and suggestions actually fire."""

    def _service(self):
        from services.memory_service import MemoryService

        return MemoryService(MagicMock())

    @pytest.mark.parametrize(
        ("filters", "expected"),
        [
            (None, []),
            ({}, []),
            ({"tags": []}, []),
            ({"tags": "python"}, []),
            ({"tags": ["python", "", 7, None, "fastapi"]}, ["python", "fastapi"]),
        ],
    )
    def test_requested_tag_filter_mirrors_the_qdrant_builder(self, filters, expected):
        """A filter the Qdrant builder ignores must not trigger this feature."""
        assert self._service()._requested_tag_filter(filters) == expected

    @pytest.mark.asyncio
    async def test_suggestions_are_skipped_when_the_recall_found_results(self):
        """The extra vocabulary query must never ride a successful recall."""
        service = self._service()
        request = MagicMock(filters={"tags": ["python"]})
        with patch("services.tag_resolution.suggest_tags", new=AsyncMock()) as spy:
            out = await service._tag_suggestions_for_empty_result(
                request,
                [MagicMock()],
                user_id=USER,
                workspace_id=WS,
                context_id=CTX,
                cross_context=False,
            )
        assert out is None
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_suggestions_are_skipped_without_a_tag_filter(self):
        service = self._service()
        request = MagicMock(filters={"type": "code"})
        with patch("services.tag_resolution.suggest_tags", new=AsyncMock()) as spy:
            out = await service._tag_suggestions_for_empty_result(
                request,
                [],
                user_id=USER,
                workspace_id=WS,
                context_id=CTX,
                cross_context=False,
            )
        assert out is None
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_suggestions_fire_on_an_empty_tag_filtered_recall(self):
        service = self._service()
        service.db = _db_with_vocabulary({"dev-env": 3})
        request = MagicMock(filters={"tags": ["dev-environment"]})
        out = await service._tag_suggestions_for_empty_result(
            request,
            [],
            user_id=USER,
            workspace_id=WS,
            context_id=CTX,
            cross_context=False,
        )
        assert out == {"dev-environment": ["dev-env (3)"]}

    @pytest.mark.asyncio
    async def test_empty_suggestions_become_none_so_the_field_is_omitted(self):
        service = self._service()
        service.db = _db_with_vocabulary({"cooking": 2})
        request = MagicMock(filters={"tags": ["kubernetes"]})
        out = await service._tag_suggestions_for_empty_result(
            request,
            [],
            user_id=USER,
            workspace_id=WS,
            context_id=CTX,
            cross_context=False,
        )
        assert out is None


class TestExpansionIsActuallyWired:
    """A helper nothing calls is a feature that does not exist.

    ``recall`` is too large to drive end-to-end here, so the wiring is asserted
    structurally: the expanded filter must be what reaches hybrid_search.
    Reverting that argument to ``request.filters`` — the exact regression — fails
    this, which a unit test of expand_tag_filter alone would not catch.
    """

    def _recall_ast(self):
        import ast
        from pathlib import Path

        import services.memory_service as memory_service

        tree = ast.parse(Path(memory_service.__file__).read_text(encoding="utf-8"))
        return ast, next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "recall"
        )

    def test_recall_calls_expand_tag_filter(self):
        ast, recall = self._recall_ast()
        called = {
            node.func.id
            for node in ast.walk(recall)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "expand_tag_filter" in called, "recall no longer expands drifted tag spellings"

    def test_hybrid_search_receives_the_expanded_filters(self):
        ast, recall = self._recall_ast()
        searches = [
            node
            for node in ast.walk(recall)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "hybrid_search"
        ]
        assert searches, "recall no longer calls hybrid_search"
        for call in searches:
            passed = {kw.arg: kw.value for kw in call.keywords}
            filters = passed.get("filters")
            assert isinstance(filters, ast.Name) and filters.id == "effective_filters", (
                "hybrid_search must receive the expanded filters, not request.filters"
            )

    def test_recall_finalize_populates_tag_suggestions(self):
        from pathlib import Path

        import services.memory_service as memory_service

        ast, _ = self._recall_ast()

        tree = ast.parse(Path(memory_service.__file__).read_text(encoding="utf-8"))
        finalize = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_recall_finalize"
        )
        responses = [
            node
            for node in ast.walk(finalize)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "RecallResponse"
        ]
        assert responses, "_recall_finalize no longer builds a RecallResponse"
        assert all(
            any(kw.arg == "tag_suggestions" for kw in call.keywords) for call in responses
        ), "RecallResponse built without tag_suggestions — the hint would never reach a caller"


class TestVocabularyIsScopedToTheCaller:
    """#1503 review: tag names and counts must never describe unreadable rows."""

    @pytest.mark.asyncio
    async def test_unshared_context_filters_by_user(self):
        db = _db_with_vocabulary({"python": 3})
        await fetch_vocabulary(db, workspace_id=WS, context_id=CTX, user_id=USER)
        sql = str(db.execute.await_args.args[0].compile(compile_kwargs={"literal_binds": True}))
        assert "user_id" in sql, (
            "a non-shared context returns only the caller's own memories to recall, "
            "so the vocabulary must be scoped the same way"
        )

    @pytest.mark.asyncio
    async def test_shared_context_aggregates_across_authors(self):
        db = _db_with_vocabulary({"python": 3})
        with _shared_context():
            await fetch_vocabulary(db, workspace_id=WS, context_id=CTX, user_id=USER)
        sql = str(db.execute.await_args.args[0].compile(compile_kwargs={"literal_binds": True}))
        assert "user_id" not in sql, "a shared context is readable by every member"

    @pytest.mark.asyncio
    async def test_suggestions_do_not_leak_another_users_tags(self):
        """End to end: the leak this fix closes."""
        db = _db_with_vocabulary({})  # caller owns nothing in this context
        assert (
            await suggest_tags(
                db, workspace_id=WS, context_id=CTX, user_id=USER, tags=["acme-migrations"]
            )
            == {}
        )


class TestCrossContextRecallSkipsSingleContextHints:
    """#1503 review: one context's vocabulary cannot describe a multi-context search."""

    def _service(self):
        from services.memory_service import MemoryService

        return MemoryService(MagicMock())

    @pytest.mark.asyncio
    async def test_suggestions_are_skipped_for_a_cross_context_recall(self):
        service = self._service()
        service.db = _db_with_vocabulary({"dev-env": 3})
        request = MagicMock(filters={"tags": ["dev-environment"]})
        with patch("services.tag_resolution.suggest_tags", new=AsyncMock()) as spy:
            out = await service._tag_suggestions_for_empty_result(
                request,
                [],
                user_id=USER,
                workspace_id=WS,
                context_id=CTX,
                cross_context=True,
            )
        assert out is None
        spy.assert_not_awaited()

    def test_expansion_is_gated_on_a_single_context(self):
        """The guard must be on the search shape, not merely on a non-None id."""
        import ast
        from pathlib import Path

        import services.memory_service as memory_service

        tree = ast.parse(Path(memory_service.__file__).read_text(encoding="utf-8"))
        recall = next(
            n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "recall"
        )
        guards = [
            n
            for n in ast.walk(recall)
            if isinstance(n, ast.If)
            and any(
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Name)
                and c.func.id == "expand_tag_filter"
                for c in ast.walk(n)
            )
        ]
        assert guards, "recall no longer expands tag filters"
        for guard in guards:
            names = {n.id for n in ast.walk(guard.test) if isinstance(n, ast.Name)}
            assert "context_ids" in names, (
                "tag expansion must be skipped for a cross-context recall — the "
                "vocabulary read covers only the primary context"
            )
