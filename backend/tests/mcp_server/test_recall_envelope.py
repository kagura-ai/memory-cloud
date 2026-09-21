"""The ``recall`` MCP envelope: shape and size (Issue #1599).

``recall`` is the hottest read an agent makes, and its result is paid for in
the calling model's context window. Three things used to inflate it:

- ``related_tags[].sample_summary`` repeated, in full, summaries already
  present in ``results[]`` — up to 10 times per response
- every result serialized ``superseded_by: null`` / ``contradicts: []`` /
  ``supersede_candidate: null`` (and a null ``context_summary``), plus a
  16-digit float ``score``
- ``\\uXXXX`` escaping of all non-ASCII text (pinned in
  ``test_response_serialization.py``)

The tests drive ``_recall_envelope`` — the function the handler serializes —
rather than a copy of it, so the size budget measures what clients receive.
"""

import inspect
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import UUID, uuid4

from mcp_server.tools import memory as mcp_memory
from mcp_server.tools._helpers import _dumps
from models.schemas import (
    MemoryResponse,
    RecallConfidence,
    RecallResponse,
    RelatedTagItem,
    SupersedeCandidate,
)
from utils.datetime import to_utc_iso

CREATED = datetime(2026, 8, 1, 9, 30, tzinfo=UTC)
UPDATED = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _memory(**overrides) -> MemoryResponse:
    fields = {
        "memory_id": uuid4(),
        "summary": "JWT expiry caused 401; fixed with refresh token rotation",
        "context_summary": "Recall when auth starts failing after a deploy",
        "type": "bug-fix",
        "importance": 0.8,
        "scope": "persistent",
        "created_at": CREATED,
        "updated_at": UPDATED,
        "client": "mcp",
        "tags": ["auth", "jwt"],
        "context": None,
        "score": 0.8287090031354203,
    }
    fields.update(overrides)
    return MemoryResponse(**fields)


def _context():
    context = MagicMock()
    context.id = UUID("00000000-0000-4000-8000-000000000001")
    context.name = "dev"
    context.display_name = "Development"
    context.is_private = False
    context.is_locked = False
    return context


def _envelope(result: RecallResponse) -> dict:
    return mcp_memory._recall_envelope(result, _context())


# ============================================================================
# related_tags: tag + count only
# ============================================================================


class TestRelatedTags:
    def test_entries_are_tag_and_count_only(self):
        result = RecallResponse(
            results=[_memory()],
            related_tags=[
                RelatedTagItem(tag="auth", count=3, sample_summary="a full summary repeated"),
                RelatedTagItem(tag="jwt", count=2, sample_summary="another full summary"),
            ],
        )
        assert _envelope(result)["related_tags"] == [
            {"tag": "auth", "count": 3},
            {"tag": "jwt", "count": 2},
        ]

    def test_no_sample_summary_anywhere_in_the_text(self):
        result = RecallResponse(
            results=[_memory()],
            related_tags=[RelatedTagItem(tag="auth", count=3, sample_summary="repeated")],
        )
        assert "sample_summary" not in _dumps(_envelope(result))

    def test_the_rest_schema_keeps_sample_summary(self):
        # MCP-surface change only: REST and the web UI still read it.
        assert "sample_summary" in RelatedTagItem.model_fields


# ============================================================================
# Result items: omit-when-empty, present values unchanged
# ============================================================================


class TestResultItems:
    def test_empty_annotations_are_absent(self):
        item = _envelope(RecallResponse(results=[_memory(context_summary=None)]))["results"][0]
        for key in ("superseded_by", "contradicts", "supersede_candidate", "context_summary"):
            assert key not in item
        assert list(item) == [
            "memory_id",
            "summary",
            "type",
            "importance",
            "scope",
            "score",
            "tags",
            "created_at",
            "updated_at",
        ]

    def test_present_annotations_are_unchanged(self):
        newer, opposing, candidate_id = uuid4(), uuid4(), uuid4()
        candidate = SupersedeCandidate(
            memory_id=candidate_id,
            summary="the older fact",
            similarity=0.97,
            detected_at=CREATED,
        )
        memory = _memory(superseded_by=newer, contradicts=[opposing], supersede_candidate=candidate)
        item = _envelope(RecallResponse(results=[memory]))["results"][0]

        assert item["context_summary"] == memory.context_summary
        assert item["superseded_by"] == str(newer)
        assert item["contradicts"] == [str(opposing)]
        # Same projection the envelope used before #1599.
        assert item["supersede_candidate"] == candidate.model_dump(mode="json")
        assert item["supersede_candidate"]["memory_id"] == str(candidate_id)

    def test_unconditional_fields_are_unchanged(self):
        memory = _memory(updated_at=None)
        item = _envelope(RecallResponse(results=[memory]))["results"][0]
        assert item["memory_id"] == str(memory.memory_id)
        assert item["summary"] == memory.summary
        assert item["type"] == "bug-fix"
        assert item["importance"] == 0.8
        assert item["scope"] == "persistent"
        assert item["tags"] == ["auth", "jwt"]
        assert item["created_at"] == to_utc_iso(CREATED)
        # updated_at stays even when null: "never edited" is itself the
        # staleness cue the tool description tells agents to read.
        assert "updated_at" in item and item["updated_at"] is None

    def test_score_is_rounded_to_four_decimals(self):
        item = _envelope(RecallResponse(results=[_memory(score=0.8287090031354203)]))["results"][0]
        assert item["score"] == 0.8287

    def test_missing_score_stays_null(self):
        item = _envelope(RecallResponse(results=[_memory(score=None)]))["results"][0]
        assert item["score"] is None

    def test_rounding_does_not_reorder_results(self):
        results = [_memory(score=0.91234), _memory(score=0.91231), _memory(score=0.5)]
        scores = [i["score"] for i in _envelope(RecallResponse(results=results))["results"]]
        assert scores == [0.9123, 0.9123, 0.5]
        assert scores == sorted(scores, reverse=True)


# ============================================================================
# Envelope-level fields are untouched
# ============================================================================


class TestEnvelope:
    def test_top_level_keys(self):
        envelope = _envelope(RecallResponse(results=[_memory()]))
        assert envelope["status"] == "success"
        assert envelope["count"] == 1
        assert envelope["context_id"] == "00000000-0000-4000-8000-000000000001"
        assert envelope["context_name"] == "dev"
        for optional in ("explore_hints", "confidence", "tag_suggestions", "degraded"):
            assert optional not in envelope

    def test_optional_signals_pass_through(self):
        confidence = RecallConfidence(level="none", result_count=0, rationale="empty pool")
        result = RecallResponse(
            results=[],
            confidence=confidence,
            tag_suggestions={"dev-env": ["dev-environment (4)"]},
            degraded=True,
            degraded_reason="embedding_unavailable",
        )
        envelope = _envelope(result)
        assert envelope["confidence"] == confidence.model_dump()
        assert envelope["tag_suggestions"] == {"dev-env": ["dev-environment (4)"]}
        assert envelope["degraded"] is True
        assert envelope["degraded_reason"] == "embedding_unavailable"

    def test_the_handler_serializes_this_envelope(self):
        # Pins the wiring: these tests would otherwise exercise an unused function.
        src = inspect.getsource(mcp_memory.handle_recall)
        assert "_recall_envelope(result, current_context)" in src
        assert "_dumps(" in src


# ============================================================================
# Size guard
# ============================================================================

# Synthetic notes in the register an agent writes them: ~280-char mixed
# Japanese/English summaries, ~150-char context summaries, 8 tags each.
_SUMMARIES = [
    "注文 API のレスポンスが遅い問題: orders テーブルの status カラムに index がなく full scan に"
    "なっていた。status と created_at の複合 index を追加して p95 が 1.2s から 80ms に改善。"
    "EXPLAIN ANALYZE で Seq Scan が Index Scan に変わったことを確認済み。書き込み性能への影響は"
    "計測の範囲では誤差程度だった。同じパターンの一覧 API が他に 2 本あるので、次のスプリントで"
    "まとめて index を見直すこと。migration は CONCURRENTLY 付きで作成する。",
    "ログイン直後に 401 が返る不具合: access token の有効期限を検証する際にサーバ間の時計ずれを"
    "考慮していなかった。leeway を 30 秒に設定し、refresh token の rotation を有効化して解消。"
    "モバイルクライアントは古い token を cache していたため、アプリ側でも 401 を受けたら一度だけ"
    "再取得する retry を入れた。再発防止として token 検証の単体テストに時計ずれのケースを"
    "追加している。監視には 401 の発生率を dashboard の panel として追加し、閾値を超えたら通知する。",
    "画像アップロードが 10MB を超えると失敗する: reverse proxy の client_max_body_size が既定値の"
    "ままだった。上限を 50MB に引き上げ、API 側でも Content-Length を検証して 413 を明示的に返す"
    "ようにした。フロントエンドは送信前にファイルサイズを確認し、超過時は圧縮を提案する。大きな"
    "ファイルは presigned URL で object storage に直接 PUT する方式へ移行する案を別途"
    "検討中である。移行する場合は upload の完了通知を webhook で受ける設計にする。",
    "夜間バッチが月初だけ timeout する: 集計クエリが月の全レコードを 1 transaction で読んでいた。"
    "日単位の chunk に分割し、chunk ごとに commit する方式へ変更して実行時間が 40 分から 6 分に"
    "短縮された。途中で失敗しても完了済みの chunk から再開できるよう、進捗を job テーブルに記録"
    "している。lock の保持時間も短くなり、日中の更新処理との競合が起きなくなったことを"
    "確認した。chunk の大きさは設定値にしてあり、件数の増加に合わせて調整できるようにしている。",
    "E2E テストが CI でだけ不安定になる: headless browser の起動直後に最初の request を送って"
    "いたのが原因。server の health check が通るまで待つ fixture を追加し、固定 sleep を condition "
    "wait に置き換えた。retry で隠すのではなく原因を直す方針。flaky だった 12 件は 50 回連続で"
    "成功することを確認済み。ローカルでは再現しないため、CI の実行ログに起動時刻を出力する"
    "ようにした。今後 sleep を含むテストは review で差し戻す運用にする。",
]

_CONTEXT_SUMMARIES = [
    "一覧系 API が遅いという報告を受けたらまず recall。index の有無を EXPLAIN で確認する手順と、"
    "複合 index の列順を決めた理由を残してある。似た構造のテーブルに同じ対処を横展開する際の"
    "チェックリストとしても使える。本番への適用は負荷の低い時間帯に行うこと。",
    "認証まわりで断続的な 401 が出たときに recall。時計ずれと token cache の二つが重なると再現が"
    "難しい。サーバ側の leeway 設定とクライアント側の retry の両方が必要だった、という結論を"
    "忘れないこと。片方だけ直した最初の修正では再発したという経緯も残してある。",
    "アップロード失敗の問い合わせが来たら recall。proxy と API とフロントエンドの三層それぞれに"
    "上限があり、どれか一つだけ直しても解消しない。presigned URL 方式への移行を判断するときの"
    "前提資料にもなる。上限値を変えるときは三層すべてを同じ PR で更新すること。",
    "バッチの timeout や lock 競合を調べるときに recall。大きな transaction を chunk に分ける"
    "判断基準と、再開可能にするための進捗記録の設計をまとめた。月初や期末など件数が跳ねる日に"
    "起きやすい。似た集計バッチを新しく書くときの雛形としても参照できる。",
    "CI だけで落ちるテストを調べるときに recall。固定 sleep や retry で隠すと再発するので、起動"
    "完了を待つ condition wait に直す。原因の切り分けに使ったログ出力の追加方法もここに"
    "書いてある。ローカルで再現しない不具合全般の調べ方としても役に立つはず。",
]

_TAGS = [
    ["category:database", "category:performance", "postgresql", "index", "slow-query"]
    + ["explain-analyze", "データベース", "性能改善"],
    ["category:auth", "category:bug-fix", "jwt", "clock-skew", "refresh-token"]
    + ["401-unauthorized", "認証", "モバイル"],
    ["category:infra", "category:api", "upload", "reverse-proxy", "request-size-limit"]
    + ["presigned-url", "アップロード", "画像"],
    ["category:batch", "category:performance", "transaction", "chunking", "timeout"]
    + ["resumable-job", "バッチ", "月初"],
    ["category:testing", "category:ci", "e2e", "flaky-test", "condition-wait"]
    + ["health-check", "テスト", "不安定"],
]

_FIXTURE_IDS = [UUID(f"00000000-0000-4000-8000-0000000001{i:02d}") for i in range(5)]
# Unrounded, as the hybrid scorer produces them.
_FIXTURE_SCORES = [0.9132847561029384, 0.8465120937465512, 0.7719034482215067]
_FIXTURE_SCORES += [0.6904418273350921, 0.6238875104462193]


def _fixture_recall() -> RecallResponse:
    """A k=5 recall as an agent sees it: 5 results, 10 related tags."""
    results = [
        MemoryResponse(
            memory_id=_FIXTURE_IDS[i],
            summary=_SUMMARIES[i],
            context_summary=_CONTEXT_SUMMARIES[i],
            type="learning",
            importance=0.85,
            scope="persistent",
            created_at=CREATED,
            updated_at=UPDATED,
            client="mcp",
            tags=_TAGS[i],
            context=None,
            score=_FIXTURE_SCORES[i],
        )
        for i in range(5)
    ]
    # As the service builds it: sample_summary is the full summary of a result
    # that is already in results[].
    related_tags = [
        RelatedTagItem(tag=_TAGS[i % 5][i % 8], count=5 - i // 2, sample_summary=_SUMMARIES[i % 5])
        for i in range(10)
    ]
    confidence = RecallConfidence(
        level="high",
        top_score=0.91,
        prominence=0.42,
        relative_margin=2.8,
        result_count=25,
        rationale="Top semantic cosine 0.91, prominence 0.42 above the candidate-pool mean.",
    )
    return RecallResponse(results=results, related_tags=related_tags, confidence=confidence)


def _legacy_text(result: RecallResponse, context) -> str:
    """The envelope exactly as it was rendered before #1599.

    Kept in the test (not in src) so the reduction below is measured against a
    fixed reference rather than asserted from memory: stdlib ``json.dumps``
    defaults, always-present null/empty annotations, unrounded score, and
    ``related_tags[].sample_summary``.
    """
    legacy = {
        "status": "success",
        "results": [
            {
                "memory_id": str(r.memory_id),
                "summary": r.summary,
                "context_summary": r.context_summary,
                "type": r.type,
                "importance": r.importance,
                "scope": r.scope,
                "score": r.score,
                "tags": r.tags,
                "created_at": to_utc_iso(r.created_at),
                "updated_at": to_utc_iso(r.updated_at),
                "superseded_by": str(r.superseded_by) if r.superseded_by else None,
                "contradicts": [str(c) for c in r.contradicts],
                "supersede_candidate": (
                    r.supersede_candidate.model_dump(mode="json") if r.supersede_candidate else None
                ),
            }
            for r in result.results
        ],
        "count": len(result.results),
        "related_tags": [
            {"tag": t.tag, "count": t.count, "sample_summary": t.sample_summary}
            for t in result.related_tags
        ],
        "context_id": str(context.id),
        "context_name": context.name,
        "context_display_name": context.display_name,
        "context_is_private": context.is_private,
        "context_is_locked": context.is_locked,
        "confidence": result.confidence.model_dump(),
    }
    return json.dumps(legacy)


# The fixture renders to 4,553 characters under the #1599 shape (22,779 the old
# way); the budget is that plus 15% headroom. Raise it only together with a
# reason — a field that earns its tokens — never to make a failing build pass.
RECALL_K5_BUDGET_CHARS = 5235


class TestSizeGuard:
    def test_fixture_matches_the_spec(self):
        fixture = _fixture_recall()
        assert len(fixture.results) == 5
        assert len(fixture.related_tags) == 10
        for r in fixture.results:
            assert 250 <= len(r.summary) <= 310, len(r.summary)
            assert 120 <= len(r.context_summary or "") <= 180, len(r.context_summary or "")
            assert len(r.tags) == 8

    def test_k5_envelope_is_within_budget(self):
        text = _dumps(_envelope(_fixture_recall()))
        assert len(text) <= RECALL_K5_BUDGET_CHARS, (
            f"k=5 recall envelope is {len(text)} chars (budget {RECALL_K5_BUDGET_CHARS}). "
            "Every character here is paid for in the caller's context window (#1599)."
        )

    def test_budget_is_not_slack(self):
        # The budget tracks the real size: more than 15% of headroom means the
        # constant went stale and would let a regression through.
        text = _dumps(_envelope(_fixture_recall()))
        assert len(text) * 1.15 >= RECALL_K5_BUDGET_CHARS - 1

    def test_k5_envelope_is_at_least_45_percent_smaller_than_before(self):
        fixture = _fixture_recall()
        new = len(_dumps(_envelope(fixture)))
        old = len(_legacy_text(fixture, _context()))
        reduction = 1 - new / old
        assert reduction >= 0.45, f"only {reduction:.1%} smaller ({old} -> {new} chars)"

    def test_nothing_an_agent_reads_was_lost(self):
        fixture = _fixture_recall()
        payload = json.loads(_dumps(_envelope(fixture)))
        assert [r["summary"] for r in payload["results"]] == _SUMMARIES
        assert [r["context_summary"] for r in payload["results"]] == _CONTEXT_SUMMARIES
        assert [r["tags"] for r in payload["results"]] == _TAGS
        assert [t["tag"] for t in payload["related_tags"]] == [t.tag for t in fixture.related_tags]
        assert payload["confidence"]["level"] == "high"


# ============================================================================
# The tool description states the shape clients actually get
# ============================================================================


class TestToolDescription:
    @staticmethod
    def _recall_tool() -> dict:
        from mcp_server.tools._definitions import get_tool_definitions

        return next(t for t in get_tool_definitions() if t["name"] == "recall")

    def test_returns_lists_related_tags_as_tag_and_count(self):
        description = self._recall_tool()["description"]
        returns = description.rsplit("Returns: {", 1)[1]
        assert "related_tags: [{tag, count}]" in returns
        assert "sample_summary" not in description

    def test_returns_marks_the_omittable_keys(self):
        returns = self._recall_tool()["description"].rsplit("Returns: {", 1)[1]
        for key in ("context_summary?", "superseded_by?", "contradicts?", "supersede_candidate?"):
            assert key in returns, f"{key} not marked optional in recall Returns"
        assert "omitted" in returns
