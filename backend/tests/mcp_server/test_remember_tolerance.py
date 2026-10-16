"""#2003: ``remember`` is tolerant on the write side.

A summary over the cap is cut at a sentence boundary and reported
(``summary_truncated``) instead of refused; a memory over 1 MB is refused as
``payload_too_large`` with the numbers; ``missing_fields`` says why a large
payload may arrive with fields missing. The agent never learns "do not write".
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools._errors import _refusal
from mcp_server.tools.batch import handle_remember_batch
from mcp_server.tools.memory import (
    MISSING_FIELDS_HELP,
    PAYLOAD_HELP,
    fit_summary,
    handle_remember,
)
from models.schemas import SUMMARY_MAX_LENGTH
from utils.exceptions import ContentSizeExceededError

SENTENCE = "The deploy script publishes the marker in place and only the restart picks it up. "


def _payload(blocks) -> dict:
    return json.loads(blocks[0].text)


# ------------------------------------------------------------------ fit_summary


def test_a_summary_within_the_cap_is_untouched():
    args = {"summary": "s" * SUMMARY_MAX_LENGTH, "content": "c", "type": "note"}
    assert fit_summary(args) == (args, {})


def test_a_long_summary_is_cut_at_the_last_sentence_end_and_kept_in_content():
    summary = (SENTENCE * 8).strip()  # 8 sentences, ~670 chars
    args = {"summary": summary, "content": "evidence line", "type": "note"}

    fitted, fields = fit_summary(args)

    assert len(fitted["summary"]) <= SUMMARY_MAX_LENGTH
    assert fitted["summary"].endswith("picks it up.")
    whole = max(k for k in range(1, 9) if len((SENTENCE * k).strip()) <= SUMMARY_MAX_LENGTH)
    assert fitted["summary"] == (SENTENCE * whole).strip()  # whole sentences only
    assert fields == {"summary_truncated": True, "summary_original_length": len(summary)}
    assert fitted["content"] == summary + "\n\nevidence line"
    assert fitted["type"] == "note" and args["summary"] == summary  # input untouched


def test_the_full_summary_is_not_prepended_twice():
    summary = (SENTENCE * 8).strip()
    fitted, _ = fit_summary({"summary": summary, "content": summary + " more", "type": "note"})
    assert fitted["content"] == summary + " more"


def test_without_a_sentence_end_the_cut_falls_on_a_word_boundary():
    summary = " ".join(["word"] * 150)  # 749 chars, no sentence end
    fitted, fields = fit_summary({"summary": summary, "content": "c", "type": "note"})
    assert len(fitted["summary"]) <= SUMMARY_MAX_LENGTH
    assert fitted["summary"].endswith("word…")
    assert fields["summary_original_length"] == len(summary)


def test_japanese_sentence_ends_count():
    sentence = "本番の色は緑で、マーカーの張り替えは不要。"
    summary = sentence * 30
    fitted, _ = fit_summary({"summary": summary, "content": "c", "type": "note"})
    assert len(fitted["summary"]) <= SUMMARY_MAX_LENGTH
    assert fitted["summary"].endswith("。")
    assert len(fitted["summary"]) % len(sentence) == 0  # whole sentences


def test_a_mostly_whitespace_summary_still_meets_the_minimum_length():
    summary = "ab" + " " * 600 + "cd"
    fitted, fields = fit_summary({"summary": summary, "content": "c", "type": "note"})
    assert 10 <= len(fitted["summary"]) <= SUMMARY_MAX_LENGTH
    assert fitted["summary"].endswith("…") and fields["summary_truncated"] is True


def test_the_prepend_check_ignores_whitespace_differences():
    summary = (SENTENCE * 8).strip()
    wrapped = summary.replace(". ", ".\r\n") + "\r\nmore"
    fitted, _ = fit_summary({"summary": summary, "content": wrapped, "type": "note"})
    assert fitted["content"] == wrapped  # already there, just wrapped differently


def test_a_period_right_at_the_cap_is_not_a_sentence_end_when_text_follows():
    # char 499 is the '.' of a version number that continues: not an end.
    summary = "a" * 300 + " " + "b" * 198 + ".5 more words follow here and on"
    fitted, _ = fit_summary({"summary": summary, "content": "c", "type": "note"})
    assert not fitted["summary"].endswith(".")
    assert fitted["summary"].endswith("…") and len(fitted["summary"]) <= SUMMARY_MAX_LENGTH


def test_trailing_whitespace_past_the_cap_is_not_a_cut():
    text = "s" * 474
    args = {"summary": text + "\n" * 30, "content": "c", "type": "note"}
    assert fit_summary(args) == (args, {})


def test_the_word_boundary_cut_keeps_a_word_that_ends_at_the_cap():
    summary = "a" * 300 + " " + "b" * 190 + " " + "ccccccc" + " " + "d" * 50
    fitted, _ = fit_summary({"summary": summary, "content": "c", "type": "note"})
    assert fitted["summary"].endswith("ccccccc…")


def test_the_prepend_is_skipped_when_it_would_cross_the_size_limit():
    summary = (SENTENCE * 8).strip()
    content = "x" * (1_000_000 - 100)
    fitted, fields = fit_summary({"summary": summary, "content": content, "type": "note"})
    assert fitted["content"] is content  # left as sent
    assert fields["summary_truncated"] is True


def test_usage_status_counts_a_size_refusal_as_413():
    from mcp_server.tools.batch import usage_status

    assert usage_status([{"status": "error", "error": "payload_too_large"}]) == 413


def test_a_non_string_summary_passes_through_for_the_schema():
    args = {"summary": 12, "content": "c", "type": "note"}
    assert fit_summary(args) == (args, {})


# ------------------------------------------------------------- error mapping


def test_the_size_refusal_is_payload_too_large_with_the_numbers():
    code, message, extra = _refusal(ContentSizeExceededError(actual=1_000_001, limit=1_000_000))
    assert code == "payload_too_large"
    assert extra == {"limit": 1_000_000, "actual": 1_000_001, "help": PAYLOAD_HELP}
    assert "1,000,001" in message and "exceeds limit" in message


@pytest.mark.asyncio
async def test_missing_fields_say_why_a_large_payload_may_lose_fields():
    payload = _payload(await handle_remember({"context_id": str(uuid4())}, "u", None))
    assert payload["error"] == "missing_fields"
    assert payload["missing_fields"] == ["summary", "content", "type"]
    assert payload["help"] == MISSING_FIELDS_HELP
    assert "1,000,000" in payload["help"]


# ------------------------------------------------------------- live handlers


async def _scope(db, owner: str):
    from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole

    db.add(User(email=f"{owner}@test.example", user_id=owner, role="user"))
    await db.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"tol-ws-{uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id=owner,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db.add(ws)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"tol-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
        trust_tier="trusted",
    )
    db.add(ctx)
    await db.flush()
    await db.commit()
    return ws, ctx


@pytest.fixture
async def live(db_session):
    """The handler's ``get_db()`` yields the test session; quota and embedding are stubbed."""

    async def _db():
        yield db_session

    quota = MagicMock(
        check_memory_quota=AsyncMock(return_value=(True, None)),
        check_memories_per_day=AsyncMock(return_value=None),
        reserve_memories_per_day=AsyncMock(return_value=None),
        release_memories_per_day=AsyncMock(return_value=None),
    )
    with (
        patch("db.base.get_db", new=_db),
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
        patch("services.quota_service.QuotaService", return_value=quota),
    ):
        yield db_session


async def _stored_summary(db, memory_id):
    from sqlalchemy import select

    from models.memory import Memory

    row = (await db.execute(select(Memory).where(Memory.id == memory_id))).scalar_one()
    return row.summary, row.content


@pytest.mark.asyncio
async def test_remember_stores_a_cut_summary_and_says_so(live):
    owner = f"tol-{uuid4().hex[:8]}"
    ws, ctx = await _scope(live, owner)
    summary = (SENTENCE * 8).strip()

    payload = _payload(
        await handle_remember(
            {"context_id": str(ctx.id), "summary": summary, "content": "c", "type": "note"},
            owner,
            ws.id,
        )
    )

    assert payload["status"] == "success"
    assert payload["summary_truncated"] is True
    assert payload["summary_original_length"] == len(summary)
    # A cut summary is 400-500 chars: the summary_long lint would tell the
    # agent to redo what the server just did, so it is muted.
    assert all(h["code"] != "summary_long" for h in payload.get("lint", []))
    stored_summary, stored_content = await _stored_summary(live, payload["memory_id"])
    assert len(stored_summary) <= SUMMARY_MAX_LENGTH and stored_summary.endswith("picks it up.")
    assert stored_content.startswith(summary)


@pytest.mark.asyncio
async def test_remember_within_the_cap_has_no_truncation_keys(live):
    owner = f"tol-{uuid4().hex[:8]}"
    ws, ctx = await _scope(live, owner)

    payload = _payload(
        await handle_remember(
            {
                "context_id": str(ctx.id),
                "summary": "s" * SUMMARY_MAX_LENGTH,
                "content": "c",
                "type": "note",
            },
            owner,
            ws.id,
        )
    )

    assert payload["status"] == "success"
    assert "summary_truncated" not in payload and "summary_original_length" not in payload


@pytest.mark.asyncio
async def test_remember_over_one_megabyte_is_payload_too_large_with_the_numbers(live):
    owner = f"tol-{uuid4().hex[:8]}"
    ws, ctx = await _scope(live, owner)

    payload = _payload(
        await handle_remember(
            {
                "context_id": str(ctx.id),
                "summary": "a summary long enough",
                "content": "x" * 1_000_001,
                "type": "note",
            },
            owner,
            ws.id,
        )
    )

    assert payload["status"] == "error" and payload["error"] == "payload_too_large"
    assert payload["limit"] == 1_000_000 and payload["actual"] > 1_000_000
    assert payload["help"] == PAYLOAD_HELP


@pytest.mark.asyncio
async def test_a_batch_item_reports_its_own_summary_cut(live):
    owner = f"tol-{uuid4().hex[:8]}"
    ws, ctx = await _scope(live, owner)
    long_item = {"summary": (SENTENCE * 8).strip(), "content": "c", "type": "note"}
    short_item = {"summary": "a reusable conclusion", "content": "c", "type": "note"}

    payload = _payload(
        await handle_remember_batch(
            {"context_id": str(ctx.id), "items": [long_item, short_item]}, owner, ws.id
        )
    )

    assert payload["status"] == "success"
    first, second = payload["results"]
    assert first["summary_truncated"] is True
    assert all(h["code"] != "summary_long" for h in first.get("lint", []))
    assert first["summary_original_length"] == len(long_item["summary"])
    assert "summary_truncated" not in second
