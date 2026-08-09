"""Write-time recall-ability lint (#1502).

Two properties matter more than coverage of the individual rules:

1. **Silence on a good write.** A lint that fires on ordinary well-formed
   memories is worse than no lint — the agent learns to ignore it. Every rule
   is therefore tested from both sides.
2. **It can never break a write.** The memory is committed before this runs, so
   the pass must swallow everything.
"""

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import SUMMARY_LONG_THRESHOLD, SUMMARY_SHORT_THRESHOLD
from services.write_lint import MAX_HINTS, lint_write

WS = uuid4()
CTX = uuid4()

GOOD_SUMMARY = (
    "JWT expiry caused intermittent 401s on the dashboard. Fixed with refresh "
    "token rotation plus clock skew handling in the auth middleware."
)


def _db_with_vocabulary(vocabulary: dict[str, int]):
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = list(vocabulary.items())
    db.execute = AsyncMock(return_value=result)
    return db


async def _lint(summary=GOOD_SUMMARY, tags=None, vocabulary=None):
    db = _db_with_vocabulary(vocabulary if vocabulary is not None else {"auth": 12})
    return await lint_write(
        db,
        workspace_id=WS,
        context_id=CTX,
        summary=summary,
        tags=tags if tags is not None else ["auth"],
    )


def _codes(hints):
    return {h.code for h in hints}


class TestSilenceOnAGoodWrite:
    @pytest.mark.asyncio
    async def test_a_well_formed_write_produces_no_hints(self):
        assert await _lint() == []

    @pytest.mark.asyncio
    async def test_an_established_tag_is_never_flagged(self):
        """A tag already in the vocabulary is by definition the right spelling."""
        hints = await _lint(tags=["dev-environment"], vocabulary={"dev-environment": 9})
        assert _codes(hints) == set()

    @pytest.mark.asyncio
    async def test_a_genuinely_new_tag_with_no_relatives_is_not_flagged(self):
        """Introducing a new topic is normal; only near-duplicates are noise."""
        hints = await _lint(tags=["kubernetes"], vocabulary={"cooking": 3, "travel": 1})
        assert _codes(hints) == set()

    @pytest.mark.asyncio
    async def test_a_summary_mentioning_a_meeting_later_is_not_narrative(self):
        """Only the OPENING establishes what a summary is about."""
        hints = await _lint(
            summary=(
                "Retry budget must reset on configuration failures, not count against "
                "the cap — agreed in the platform meeting after the outage."
            )
        )
        assert "summary_narrative" not in _codes(hints)


class TestSummaryRules:
    @pytest.mark.asyncio
    async def test_short_summary_is_flagged_with_the_schema_threshold(self):
        hints = await _lint(summary="x" * (SUMMARY_SHORT_THRESHOLD - 1))
        assert "summary_short" in _codes(hints)
        assert str(SUMMARY_SHORT_THRESHOLD) in next(
            h.hint for h in hints if h.code == "summary_short"
        )

    @pytest.mark.asyncio
    async def test_at_the_threshold_is_not_short(self):
        assert "summary_short" not in _codes(await _lint(summary="x" * SUMMARY_SHORT_THRESHOLD))

    @pytest.mark.asyncio
    async def test_overlong_summary_is_flagged(self):
        hints = await _lint(summary="x" * (SUMMARY_LONG_THRESHOLD + 1))
        assert "summary_long" in _codes(hints)

    @pytest.mark.asyncio
    async def test_at_the_threshold_is_not_long(self):
        assert "summary_long" not in _codes(await _lint(summary="x" * SUMMARY_LONG_THRESHOLD))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "summary",
        [
            "Discussed auth errors in today's meeting and went over the retry budget.",
            "Talked about the deployment pipeline and what to change next quarter.",
            "Meeting notes on the embedding backlog and how we plan to drain it.",
            "今日は認証エラーについて話し合った。リトライ予算の扱いを検討している。",
            "打ち合わせで埋め込みのバックログについて確認し、次の対応を検討した。",
        ],
    )
    async def test_event_record_openings_are_flagged(self, summary):
        assert "summary_narrative" in _codes(await _lint(summary=summary))

    @pytest.mark.asyncio
    async def test_conclusion_first_summaries_are_not_flagged(self):
        for summary in [
            "PostgreSQL JSONB GIN index cut the dashboard query from 3.2s to 40ms.",
            "認証エラーはJWT期限切れが原因。リフレッシュトークン回転で解消した。",
            "Merge losers are soft-deleted, so their id stops resolving after dedup.",
        ]:
            assert "summary_narrative" not in _codes(await _lint(summary=summary))


class TestTagRules:
    @pytest.mark.asyncio
    async def test_missing_tags_are_flagged(self):
        hints = await _lint(tags=[])
        assert "no_tags" in _codes(hints)

    @pytest.mark.asyncio
    async def test_no_tags_skips_the_vocabulary_read_entirely(self):
        """Nothing to compare — do not pay for the query."""
        db = _db_with_vocabulary({"auth": 3})
        await lint_write(db, workspace_id=WS, context_id=CTX, summary=GOOD_SUMMARY, tags=[])
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_near_duplicate_tag_is_flagged_with_the_established_spelling(self):
        hints = await _lint(tags=["dev-env"], vocabulary={"dev-environment": 12, "auth": 3})
        hint = next(h for h in hints if h.code == "tag_near_duplicate")
        assert hint.subject == "dev-env"
        assert "dev-environment" in hint.hint
        assert "12" in hint.hint

    @pytest.mark.asyncio
    async def test_the_most_used_relative_is_the_one_suggested(self):
        """Point the writer at the dominant spelling, not an equally rare one."""
        hints = await _lint(
            tags=["troubleshootin"],
            vocabulary={"troubleshooting": 40, "troubleshootng": 2},
        )
        assert "troubleshooting" in next(h.hint for h in hints if h.code == "tag_near_duplicate")

    @pytest.mark.asyncio
    async def test_mechanical_variant_of_an_existing_tag_is_flagged(self):
        hints = await _lint(tags=["Dev_Environment"], vocabulary={"dev-environment": 7})
        assert "tag_near_duplicate" in _codes(hints)


class TestItCanNeverBreakAWrite:
    @pytest.mark.asyncio
    async def test_a_broken_vocabulary_read_yields_no_hints(self):
        db = MagicMock()
        db.execute = AsyncMock(side_effect=RuntimeError("boom"))
        assert (
            await lint_write(db, workspace_id=WS, context_id=CTX, summary=GOOD_SUMMARY, tags=["x"])
            == []
        )

    @pytest.mark.asyncio
    async def test_an_internal_error_yields_no_hints(self):
        with patch("services.write_lint._summary_hints", side_effect=RuntimeError("boom")):
            assert await _lint() == []

    @pytest.mark.asyncio
    async def test_hints_are_bounded(self):
        vocabulary = {f"tag-{i}": i + 1 for i in range(30)}
        hints = await _lint(
            summary="short",
            tags=[f"tag{i}" for i in range(30)],
            vocabulary=vocabulary,
        )
        assert len(hints) <= MAX_HINTS

    @pytest.mark.asyncio
    async def test_non_string_tags_are_ignored(self):
        hints = await _lint(tags=["auth", 7, None, ""], vocabulary={"auth": 5})
        assert _codes(hints) == set()


class TestServiceGuards:
    """The service wrapper's own preconditions."""

    def _service(self):
        from services.memory_service import MemoryService

        return MemoryService(MagicMock())

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("workspace_id", "context_id", "summary"),
        [
            (None, CTX, GOOD_SUMMARY),
            (WS, None, GOOD_SUMMARY),
            (WS, CTX, None),
            (WS, CTX, ""),
        ],
    )
    async def test_unlocatable_writes_are_not_linted(self, workspace_id, context_id, summary):
        """Without a context the vocabulary comparison is meaningless."""
        with patch("services.write_lint.lint_write", new=AsyncMock()) as spy:
            out = await self._service()._lint_write(
                workspace_id=workspace_id,
                context_id=context_id,
                summary=summary,
                tags=["auth"],
            )
        assert out == []
        spy.assert_not_awaited()


class TestWiring:
    """A hint nobody receives is not a feature."""

    def _service_ast(self):
        import services.memory_service as memory_service

        return ast.parse(Path(memory_service.__file__).read_text(encoding="utf-8"))

    def test_every_write_response_construction_populates_lint(self):
        tree = self._service_ast()
        sites = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"RememberResponse", "UpdateMemoryResponse"}
        ]
        assert len(sites) >= 3, f"expected the known write-response sites, found {len(sites)}"
        for call in sites:
            assert any(kw.arg == "lint" for kw in call.keywords), (
                f"{call.func.id} at line {call.lineno} is built without lint — "
                "the hints would never reach a caller"
            )

    def test_lint_runs_after_the_commit(self):
        """Ordering is the safety property: the write must already be durable."""
        tree = self._service_ast()
        remember = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "remember"
        )
        commits = [
            node.lineno
            for node in ast.walk(remember)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "commit"
        ]
        lints = [
            node.lineno
            for node in ast.walk(remember)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_lint_write"
        ]
        assert commits and lints
        assert min(lints) > min(commits), "lint must run after the memory is committed"

    @pytest.mark.asyncio
    async def test_mcp_helper_omits_the_key_on_a_clean_write(self):
        from mcp_server.tools._helpers import _lint_response_field

        assert _lint_response_field([]) == {}
        assert _lint_response_field(None) == {}

    @pytest.mark.asyncio
    async def test_mcp_helper_emits_hints_without_null_subjects(self):
        from mcp_server.tools._helpers import _lint_response_field
        from models.schemas import WriteLintHint

        out = _lint_response_field(
            [
                WriteLintHint(code="no_tags", hint="add tags"),
                WriteLintHint(code="tag_near_duplicate", hint="reuse 'auth'", subject="authh"),
            ]
        )
        assert out["lint"][0] == {"code": "no_tags", "hint": "add tags"}
        assert out["lint"][1]["subject"] == "authh"
