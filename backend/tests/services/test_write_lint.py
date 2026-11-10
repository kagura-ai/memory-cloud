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
from services.tag_resolution import clear_vocabulary_cache
from services.write_lint import MAX_HINTS, lint_write

WS = uuid4()
CTX = uuid4()
USER = "caller-1"


pytestmark = pytest.mark.usefixtures("no_identity_links")


@pytest.fixture(autouse=True)
def _unshared_context():
    """Vocabulary reads resolve context sharing; default to the scoped path."""
    with patch("services.context_service.ContextService") as cls:
        cls.return_value.is_context_shared = AsyncMock(return_value=False)
        yield cls


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
        user_id=USER,
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
        await lint_write(
            db, workspace_id=WS, context_id=CTX, user_id=USER, summary=GOOD_SUMMARY, tags=[]
        )
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

    @pytest.mark.asyncio
    async def test_kana_variant_of_an_existing_tag_is_flagged(self):
        """#2028: hiragana / katakana and a word-final ``ー`` are one tag."""
        hints = await _lint(tags=["でぷろい", "サーバー"], vocabulary={"デプロイ": 6, "サーバ": 4})
        flagged = {h.subject: h.hint for h in hints if h.code == "tag_near_duplicate"}
        assert set(flagged) == {"でぷろい", "サーバー"}
        assert "'デプロイ'" in flagged["でぷろい"]
        assert "'サーバ'" in flagged["サーバー"]

    @pytest.mark.asyncio
    async def test_an_inner_segment_mark_is_a_near_duplicate_not_a_specialisation(self):
        """#2028: ``サーバー-ログ`` keeps its inner mark, so next to a stored
        ``サーバ-ログ`` it is one edit away and hinted, never silenced."""
        hints = await _lint(tags=["サーバー-ログ"], vocabulary={"サーバ-ログ": 3})
        assert [h.subject for h in hints if h.code == "tag_near_duplicate"] == ["サーバー-ログ"]

    @pytest.mark.asyncio
    async def test_numbered_tags_are_not_flagged_against_their_own_series(self):
        """#1608: a new issue / PR / version / date tag is not a misspelling.

        The real near-duplicate in the same write still fires, so the silence is
        the rule and not an accident of the vocabulary.
        """
        hints = await _lint(
            tags=["issue:#1599", "pr:#1607", "v0.73.0", "session-2026-09-21", "dev-env"],
            vocabulary={
                "issue:#179": 4,
                "pr:#601": 2,
                "v0.69.0": 9,
                "session-2026-05-20": 3,
                "dev-environment": 12,
            },
        )
        flagged = [h.subject for h in hints if h.code == "tag_near_duplicate"]
        assert flagged == ["dev-env"]

    @pytest.mark.asyncio
    async def test_a_misspelled_numbered_tag_is_still_flagged(self):
        """Digits AND letters differ: that is a typo, not the next issue number."""
        hints = await _lint(tags=["isue:#1599"], vocabulary={"issue:#1599": 5, "issue:#179": 4})
        hint = next(h for h in hints if h.code == "tag_near_duplicate")
        assert hint.subject == "isue:#1599"
        assert "'issue:#1599'" in hint.hint

    @pytest.mark.asyncio
    async def test_the_same_number_padded_differently_is_still_flagged(self):
        """``sprint-7`` for a stored ``sprint-07`` is drift, not the next sprint."""
        hints = await _lint(tags=["sprint-7"], vocabulary={"sprint-07": 5, "sprint-06": 9})
        hint = next(h for h in hints if h.code == "tag_near_duplicate")
        assert hint.subject == "sprint-7"
        assert "'sprint-07'" in hint.hint
        assert "sprint-06" not in hint.hint


class TestCompoundTagsAreNotNearDuplicatesOfTheirOwnSegments:
    """#1617: ``session-cookie`` is a sub-topic of ``session``, not a spelling of
    it. Acting on the hint would discard information, and once a context holds
    the short generic tags almost every new compound tag would draw one.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("new", "stored"),
        [
            ("session-cookie", "session"),
            ("deploy-checklist", "deploy"),
            ("cache-layer-redis", "cache-layer"),
            ("benchmark-results", "benchmark"),
            ("NEXT_PUBLIC_PLAN_DISPLAY_NAMES", "next-public"),
            ("session-2026-09-11", "session"),
            ("some-repo#62", "some-repo"),
            ("issue:#1599", "issue"),
            ("category:auth", "category"),
            ("s3-bucket-2", "s3-bucket"),
            ("sessions", "session-cookie"),
            ("ｓｅｓｓｉｏｎ－ｃｏｏｋｉｅ", "session"),
            ("セッション-クッキー", "セッション"),
        ],
    )
    async def test_a_specialisation_is_silent_in_both_directions(self, new, stored):
        assert await _lint(tags=[new], vocabulary={stored: 30}) == []
        # The vocabulary is cached per context (#1512) and the first write went
        # through to it, so the reverse direction needs a fresh vocabulary or the
        # written tag would count as established and the test would pass for the
        # wrong reason.
        clear_vocabulary_cache()
        assert await _lint(tags=[stored], vocabulary={new: 30}) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("new", "stored"),
        [
            ("dev-env", "dev-environment"),
            ("oauth", "oauth2"),
            ("v0.73", "v0.73.0"),
            ("session-2026-09", "session-2026-09-21"),
            ("plan-tier", "plan-tiers"),
            ("isue:#1599", "issue:#1599"),
            ("sprint-07", "sprint-7"),
            ("Dev_Environment", "dev-environment"),
            ("deploy-check", "deploy-checklist"),
            ("node", "node.js"),
            ("next", "next.js"),
            ("category:auth", "category:authn"),
            ("category:auth", "category-auth"),
            ("auth-n", "authn"),
        ],
    )
    async def test_abbreviations_typos_and_variants_are_still_flagged(self, new, stored):
        hints = await _lint(tags=[new], vocabulary={stored: 5})
        assert [h.subject for h in hints if h.code == "tag_near_duplicate"] == [new]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("new", "stored"),
        [
            ("auth", "auth-n"),
            ("front", "front-end"),
            ("mongo", "mongo-db"),
            ("python", "python-3"),
            ("oauth", "oauth-2.0"),
        ],
    )
    async def test_a_respelling_on_a_segment_boundary_goes_silent_deliberately(self, new, stored):
        """Accepted trade-off: indistinguishable from ``java`` / ``java-script``.
        The unseparated spelling of the same pair still hints."""
        assert await _lint(tags=[new], vocabulary={stored: 5}) == []
        clear_vocabulary_cache()  # the first write went through to the cached vocabulary
        hints = await _lint(tags=[new], vocabulary={stored.replace("-", ""): 5})
        assert "tag_near_duplicate" in _codes(hints)

    @pytest.mark.asyncio
    async def test_the_generic_tag_no_longer_masks_the_real_variant(self):
        """The highest-count match used to win outright, and the generic tag
        almost always out-counts the mechanical variant it hides."""
        hints = await _lint(
            tags=["session-cookie"], vocabulary={"session": 30, "session_cookie": 2}
        )
        hint = next(h for h in hints if h.code == "tag_near_duplicate")
        assert "'session_cookie'" in hint.hint
        assert "'session'" not in hint.hint

    @pytest.mark.asyncio
    async def test_an_equal_fold_match_is_picked_ahead_of_a_prefix_match(self):
        """Class C (``AuthContext`` / ``auth``) stays a match, so the filter alone
        would still report ``auth``; the equal fold is the stronger claim."""
        hints = await _lint(tags=["AuthContext"], vocabulary={"auth": 50, "auth_context": 2})
        hint = next(h for h in hints if h.code == "tag_near_duplicate")
        assert "'auth_context'" in hint.hint

    @pytest.mark.asyncio
    async def test_without_an_equal_fold_the_most_used_match_still_wins(self):
        hints = await _lint(
            tags=["troubleshootin"],
            vocabulary={"troubleshooting": 40, "troubleshootng": 2},
        )
        assert "'troubleshooting'" in next(h.hint for h in hints if h.code == "tag_near_duplicate")

    @pytest.mark.asyncio
    async def test_the_real_near_duplicate_in_the_same_write_still_fires(self):
        hints = await _lint(
            tags=["session-cookie", "deploy-checklist", "dev-env"],
            vocabulary={"session": 30, "deploy": 12, "dev-environment": 12},
        )
        assert [h.subject for h in hints if h.code == "tag_near_duplicate"] == ["dev-env"]


class TestItCanNeverBreakAWrite:
    @pytest.mark.asyncio
    async def test_a_broken_vocabulary_read_yields_no_hints(self):
        db = MagicMock()
        db.execute = AsyncMock(side_effect=RuntimeError("boom"))
        assert (
            await lint_write(
                db,
                workspace_id=WS,
                context_id=CTX,
                user_id=USER,
                summary=GOOD_SUMMARY,
                tags=["x"],
            )
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
                user_id=USER,
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

        def method(name):
            return next(
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef) and node.name == name
            )

        def calls(fn, attr):
            return [
                node.lineno
                for node in ast.walk(fn)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == attr
            ]

        # #1853: remember = prepare → create → commit → finish; lint lives in
        # the finish step, which remember calls only after its commit.
        remember = method("remember")
        commits = calls(remember, "commit")
        finishes = calls(remember, "_finish_remember")
        assert commits and finishes
        assert min(finishes) > min(commits), "finish (and its lint) must follow the commit"
        assert calls(method("_finish_remember"), "_lint_write"), "lint runs in the finish step"
        assert not calls(method("_prepare_remember"), "_lint_write"), "never before the commit"

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


class TestLintCanNeverFailACommittedWrite:
    """#1502 review: the import sits outside lint_write's own guard."""

    def _service(self):
        from services.memory_service import MemoryService

        return MemoryService(MagicMock())

    @pytest.mark.asyncio
    async def test_an_unimportable_lint_module_yields_no_hints(self):
        """remember() runs this INSIDE the try that rolls back and re-raises."""
        import builtins

        real_import = builtins.__import__

        def boom(name, *args, **kwargs):
            if name == "services.write_lint":
                raise ImportError("simulated broken import chain")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=boom):
            out = await self._service()._lint_write(
                workspace_id=WS,
                context_id=CTX,
                user_id=USER,
                summary=GOOD_SUMMARY,
                tags=["auth"],
            )
        assert out == []


class TestVocabularyIsScopedToTheCaller:
    @pytest.mark.asyncio
    async def test_hint_cannot_describe_another_users_tags(self):
        """The lint reads the same vocabulary recall does, scoped the same way."""
        db = _db_with_vocabulary({})
        hints = await lint_write(
            db,
            workspace_id=WS,
            context_id=CTX,
            user_id=USER,
            summary=GOOD_SUMMARY,
            tags=["dev-env"],
        )
        assert [h.code for h in hints] == []


class TestVocabularyCacheOnTheWritePath:
    """#1512: lint reads a pre-write snapshot through the per-context TTL cache."""

    @pytest.mark.asyncio
    async def test_repeated_writes_share_one_aggregate(self):
        db = _db_with_vocabulary({"auth": 12})
        for _ in range(3):
            await lint_write(
                db,
                workspace_id=WS,
                context_id=CTX,
                user_id=USER,
                summary=GOOD_SUMMARY,
                tags=["Auth"],
            )
        assert db.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_untagged_write_reads_no_vocabulary(self):
        db = _db_with_vocabulary({"auth": 12})
        await lint_write(
            db, workspace_id=WS, context_id=CTX, user_id=USER, summary=GOOD_SUMMARY, tags=[]
        )
        assert db.execute.await_count == 0

    @pytest.mark.asyncio
    async def test_drifted_spelling_fires_on_a_miss_and_on_a_hit(self):
        """The row is committed before lint, so a raw aggregate would contain the
        drifted tag and suppress the hint on a miss only — the rule must not depend
        on cache warmth."""
        # Miss: the aggregate already counts this write's 'Auth'.
        db = _db_with_vocabulary({"auth": 12, "Auth": 1})
        miss = await lint_write(
            db, workspace_id=WS, context_id=CTX, user_id=USER, summary=GOOD_SUMMARY, tags=["Auth"]
        )
        assert [h.code for h in miss] == ["tag_near_duplicate"]
        # Hit, from a different writer of a genuinely new drift: still fires.
        hit = await lint_write(
            db, workspace_id=WS, context_id=CTX, user_id=USER, summary=GOOD_SUMMARY, tags=["AUTH"]
        )
        assert [h.code for h in hit] == ["tag_near_duplicate"]
        assert db.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_second_identical_write_sees_the_first_as_established(self):
        """Write-through: once 'Auth' has been written it is a stored spelling,
        so repeating it within the TTL is not a spurious near-duplicate."""
        db = _db_with_vocabulary({"auth": 12, "Auth": 1})
        await lint_write(
            db, workspace_id=WS, context_id=CTX, user_id=USER, summary=GOOD_SUMMARY, tags=["Auth"]
        )
        again = await lint_write(
            db, workspace_id=WS, context_id=CTX, user_id=USER, summary=GOOD_SUMMARY, tags=["Auth"]
        )
        assert again == []
        assert db.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_logs_hints_only_when_there_are_any(self):
        db = _db_with_vocabulary({"auth": 12})
        mid = uuid4()
        with patch("services.write_lint.logger") as log:
            hints = await lint_write(
                db,
                workspace_id=WS,
                context_id=CTX,
                user_id=USER,
                summary=GOOD_SUMMARY,
                tags=["Auth"],
                memory_id=mid,
            )
            await lint_write(
                db,
                workspace_id=WS,
                context_id=CTX,
                user_id=USER,
                summary=GOOD_SUMMARY,
                tags=["auth"],
            )
        assert [h.code for h in hints] == ["tag_near_duplicate"]
        calls = [c for c in log.info.call_args_list if c.args[0] == "write_lint_hints"]
        assert len(calls) == 1, "a clean write logs nothing"
        kw = calls[0].kwargs
        assert kw["memory_id"] == str(mid) and kw["context_id"] == str(CTX)
        assert kw["codes"] == ["tag_near_duplicate"] and kw["tag_near_duplicate_hints"] == 1

    @pytest.mark.asyncio
    async def test_hint_count_is_taken_after_truncation(self):
        """The logged fire count must describe what the caller receives."""
        db = _db_with_vocabulary({f"tag{i}": 5 for i in range(10)})
        drifted = [f"Tag{i}" for i in range(10)]
        with patch("services.write_lint.logger") as log:
            hints = await lint_write(
                db, workspace_id=WS, context_id=CTX, user_id=USER, summary="short", tags=drifted
            )
        assert len(hints) == MAX_HINTS
        kw = [c for c in log.info.call_args_list if c.args[0] == "write_lint_hints"][0].kwargs
        assert kw["tag_near_duplicate_hints"] == sum(
            1 for h in hints if h.code == "tag_near_duplicate"
        )
        assert len(kw["codes"]) == MAX_HINTS


class TestCheckedTags:
    """#2585: an add_tags / remove_tags call lints the tags it added only."""

    @pytest.mark.asyncio
    async def test_only_the_checked_tags_are_compared(self):
        # 'Auth' is a drifted spelling the memory already carried; the call
        # added 'deploy', which is fine — nothing to say about either.
        db = _db_with_vocabulary({"auth": 12, "Auth": 1, "deploy": 3})
        hints = await lint_write(
            db,
            workspace_id=WS,
            context_id=CTX,
            user_id=USER,
            summary=GOOD_SUMMARY,
            tags=["Auth", "deploy"],
            checked_tags=["deploy"],
        )
        assert hints == []

    @pytest.mark.asyncio
    async def test_an_added_drift_still_fires(self):
        db = _db_with_vocabulary({"auth": 12, "Auth": 1})
        hints = await lint_write(
            db,
            workspace_id=WS,
            context_id=CTX,
            user_id=USER,
            summary=GOOD_SUMMARY,
            tags=["release", "Auth"],
            checked_tags=["Auth"],
        )
        assert [(h.code, h.subject) for h in hints] == [("tag_near_duplicate", "Auth")]

    @pytest.mark.asyncio
    async def test_nothing_added_reads_no_vocabulary_and_is_not_untagged(self):
        db = _db_with_vocabulary({"auth": 12})
        hints = await lint_write(
            db,
            workspace_id=WS,
            context_id=CTX,
            user_id=USER,
            summary=GOOD_SUMMARY,
            tags=["Auth"],
            checked_tags=[],
        )
        assert hints == []
        assert db.execute.await_count == 0

    @pytest.mark.asyncio
    async def test_a_call_that_removed_every_tag_is_untagged(self):
        db = _db_with_vocabulary({"auth": 12})
        hints = await lint_write(
            db,
            workspace_id=WS,
            context_id=CTX,
            user_id=USER,
            summary=GOOD_SUMMARY,
            tags=[],
            checked_tags=[],
        )
        assert _codes(hints) == {"no_tags"}


# ------------------------------------------------- event_date_mismatch (#2752)

LAST_WEEK = {
    "observed_at": "2024-03-11",
    "event_phrase": "last week",
    "event_start": "2024-03-04",
    "event_end": "2024-03-10",
}
YESTERDAY = {
    "observed_at": "2024-03-11",
    "event_phrase": "yesterday",
    "event_start": "2024-03-10",
    "event_end": "2024-03-10",
}


async def _event_lint(summary, details, content=None):
    return await lint_write(
        _db_with_vocabulary({"auth": 12}),
        workspace_id=WS,
        context_id=CTX,
        user_id=USER,
        summary=summary,
        tags=["auth"],
        content=content,
        details=details,
    )


def _mismatch(hints):
    return [h for h in hints if h.code == "event_date_mismatch"]


class TestEventDateMismatch:
    """A date written in parentheses right after the phrase that differs from the
    resolved days is reported; nothing is rewritten and the write stands."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "written",
        [
            "2024-03-05..2024-03-11",
            "2024-03-05 to 2024-03-11",
            "2024-03-05–2024-03-11",
            "2024-03-05—2024-03-11",
            "2024-03-05-2024-03-11",
            "2024-03-05/2024-03-11",
            "2024-03-05 – 2024-03-11",
            "2024-03-04",  # one date for a seven-day range
        ],
    )
    async def test_a_differing_date_or_range_after_the_phrase_fires(self, written):
        summary = f"Shipped the new parser last week ({written}) after the review round."
        (hint,) = _mismatch(await _event_lint(summary, dict(LAST_WEEK)))
        assert hint.subject == "last week"
        # The written days as parsed, not the client's text.
        shown = written if written == "2024-03-04" else "2024-03-05..2024-03-11"
        assert f"'last week' is written as ({shown})" in hint.hint
        assert "2024-03-04..2024-03-10" in hint.hint

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "written",
        [
            "2024-03-04..2024-03-10",
            "2024-03-04 to 2024-03-10",
            "2024-03-04–2024-03-10",
            "2024-03-04—2024-03-10",
            "2024-03-04-2024-03-10",
            "2024-03-04/2024-03-10",
        ],
    )
    async def test_an_agreeing_range_is_silent(self, written):
        summary = f"Shipped the new parser last week ({written}) after the review round."
        assert _mismatch(await _event_lint(summary, dict(LAST_WEEK))) == []

    @pytest.mark.asyncio
    async def test_one_date_agrees_with_a_single_day(self):
        ok = "Rolled the deploy back yesterday (2024-03-10) after the alert."
        assert _mismatch(await _event_lint(ok, dict(YESTERDAY))) == []
        bad = "Rolled the deploy back yesterday (2024-03-09) after the alert."
        (hint,) = _mismatch(await _event_lint(bad, dict(YESTERDAY)))
        assert "2024-03-09" in hint.hint and "2024-03-10" in hint.hint

    @pytest.mark.asyncio
    async def test_case_insensitive_and_the_space_is_optional(self):
        for summary in (
            "Last Week (2024-03-05..2024-03-11) the parser shipped after review.",
            "The parser shipped LAST WEEK(2024-03-05..2024-03-11) after review.",
        ):
            assert len(_mismatch(await _event_lint(summary, dict(LAST_WEEK)))) == 1

    @pytest.mark.asyncio
    async def test_the_content_is_read_too(self):
        hints = await _event_lint(
            GOOD_SUMMARY, dict(LAST_WEEK), content="It shipped last week (2024-03-11)."
        )
        assert len(_mismatch(hints)) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "summary",
        [
            "Shipped the new parser last week after the review round, on schedule.",
            "Shipped the new parser last week (stated on 2024-03-11) after review.",
            "Shipped the new parser last week (2024-03-04–32) after the review round.",
            "Shipped the new parser last week (2024-02-30) after the review round.",
            "Shipped the new parser last week (around 2024-03-05) after the review.",
            "Shipped the new parser on 2024-03-05 (2024-03-05) after the review.",
        ],
    )
    async def test_silent_without_a_bare_date_after_the_phrase(self, summary):
        assert _mismatch(await _event_lint(summary, dict(LAST_WEEK))) == []

    @pytest.mark.asyncio
    async def test_silent_when_the_phrase_did_not_resolve(self):
        summary = "Shipped the new parser a while back (2024-03-05) after the review round."
        unresolved = {"observed_at": "2024-03-11", "event_phrase": "a while back"}
        assert _mismatch(await _event_lint(summary, unresolved)) == []
        no_anchor = {"event_phrase": "last week"}
        summary = "Shipped the new parser last week (2024-03-05) after the review round."
        assert _mismatch(await _event_lint(summary, no_anchor)) == []
        assert _mismatch(await _event_lint(summary, None)) == []
        pair_only = {"event_start": "2024-03-04", "event_end": "2024-03-10"}
        assert _mismatch(await _event_lint(summary, pair_only)) == []

    @pytest.mark.asyncio
    async def test_the_direction_is_counted(self):
        details = {
            "observed_at": "2024-03-09",
            "event_phrase": "on Sunday",
            "event_direction": "future",
            "event_start": "2024-03-10",
            "event_end": "2024-03-10",
        }
        ok = "The team goes hiking on Sunday (2024-03-10) near the lake."
        assert _mismatch(await _event_lint(ok, details)) == []
        bad = "The team goes hiking on Sunday (2024-03-03) near the lake."
        assert len(_mismatch(await _event_lint(bad, details))) == 1

    @pytest.mark.parametrize(
        ("written", "fires"),
        [
            # The skills' own form: the second date keeps only what differs.
            ("2024-03-04–10", False),
            ("2024-03-04..10", False),
            ("2024-03-04 to 10", False),
            ("2024-03-04-10", False),
            ("2024-03-04 – 03-10", False),
            ("2024-03-05–11", True),
            ("2024-03-05..11", True),
            ("2024-03-04-09", True),
            ("2024-03-04/03-11", True),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_shortened_second_date_inherits_the_first_ones_parts(self, written, fires):
        """The range form the skills teach, "(2024-03-09–10)", is read too."""
        summary = f"Shipped the new parser last week ({written}) after the review round."
        hints = _mismatch(await _event_lint(summary, dict(LAST_WEEK)))
        assert len(hints) == (1 if fires else 0)
        if fires:
            assert "but the memory holds 2024-03-04..2024-03-10" in hints[0].hint

    @pytest.mark.asyncio
    async def test_a_month_day_second_date_crosses_the_month(self):
        details = {
            "observed_at": "2024-03-06",
            "event_phrase": "last week",
            "event_start": "2024-02-26",
            "event_end": "2024-03-03",
        }
        ok = "Shipped the new parser last week (2024-02-26–03-03) after the review round."
        assert _mismatch(await _event_lint(ok, details)) == []
        bad = "Shipped the new parser last week (2024-02-26–03-02) after the review round."
        assert len(_mismatch(await _event_lint(bad, details))) == 1

    @pytest.mark.asyncio
    async def test_a_leading_on_in_or_the_of_the_phrase_is_optional_in_the_text(self):
        details = {
            "observed_at": "2024-03-09",
            "event_phrase": "on Sunday",
            "event_start": "2024-03-03",
            "event_end": "2024-03-03",
        }
        bad = "The team went hiking Sunday (2024-03-10) near the lake."
        assert len(_mismatch(await _event_lint(bad, details))) == 1
        ok = "The team went hiking on Sunday (2024-03-03) near the lake."
        assert _mismatch(await _event_lint(ok, details)) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("phrase", "start", "end", "summary"),
        [
            (
                "tomorrow",
                "2024-03-10",
                "2024-03-10",
                "We fly out the day after tomorrow (2024-03-11).",
            ),
            (
                "yesterday",
                "2024-03-08",
                "2024-03-08",
                "It broke the day before yesterday (2024-03-07).",
            ),
            ("Sunday", "2024-03-03", "2024-03-03", "The review moved to next Sunday (2024-03-17)."),
            ("March", "2024-03-01", "2024-03-31", "The launch slips to next March (2025-03-01)."),
            ("may", "2023-05-01", "2023-05-31", "We may (2024-01-01) go to the coast after all."),
        ],
    )
    async def test_the_phrase_inside_a_longer_expression_is_not_it(
        self, phrase, start, end, summary
    ):
        """The stored phrase as the tail of another expression ("the day after
        tomorrow" for "tomorrow") or a common word ("We may") is not the phrase."""
        details = {
            "observed_at": "2024-03-09",
            "event_phrase": phrase,
            "event_start": start,
            "event_end": end,
        }
        assert _mismatch(await _event_lint(summary, details)) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("phrase", "start", "end", "summary"),
        [
            (
                "last weekend",
                "2024-03-02",
                "2024-03-03",
                "We met last weekend (2024-03-01) at the lake.",
            ),
            ("Sunday", "2024-03-03", "2024-03-03", "The team went hiking on Sunday (2024-03-05)."),
            ("Sunday", "2024-03-03", "2024-03-03", "Hiking. Sunday (2024-03-05) was cold."),
        ],
    )
    async def test_a_real_mismatch_still_fires(self, phrase, start, end, summary):
        details = {
            "observed_at": "2024-03-09",
            "event_phrase": phrase,
            "event_start": start,
            "event_end": end,
        }
        assert len(_mismatch(await _event_lint(summary, details))) == 1

    @pytest.mark.asyncio
    async def test_the_hint_stays_short_whatever_the_text(self):
        gap = " " * 100_000
        content = f"It shipped last{gap}week ({gap}2024-03-05{gap}..{gap}2024-03-11{gap})."
        (hint,) = _mismatch(await _event_lint(GOOD_SUMMARY, dict(LAST_WEEK), content=content))
        assert len(hint.hint) < 300
        assert "'last week'" in hint.hint and "2024-03-05..2024-03-11" in hint.hint

    @pytest.mark.asyncio
    async def test_it_compares_with_the_stored_days_not_a_new_resolution(self):
        stored = {
            "event_phrase": "three years ago",
            "observed_at": "2024-03-09",
            "event_start": "2021-01-01",
            "event_end": "2021-12-31",
        }
        agrees = "Moved to Osaka three years ago (2021-01-01..2021-12-31) for the job."
        assert _mismatch(await _event_lint(agrees, stored)) == []
        differs = "Moved to Osaka three years ago (2021-03-09) for the job."
        (hint,) = _mismatch(await _event_lint(differs, stored))
        assert "2021-01-01..2021-12-31" in hint.hint
        no_days = {"event_phrase": "three years ago", "observed_at": "2024-03-09"}
        assert _mismatch(await _event_lint(differs, no_days)) == []

    @pytest.mark.asyncio
    async def test_a_well_formed_write_stays_silent(self):
        summary = "Shipped the new parser last week (2024-03-04..2024-03-10) after review."
        assert await _event_lint(summary, dict(LAST_WEEK)) == []
