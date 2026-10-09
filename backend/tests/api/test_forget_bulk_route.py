"""``POST /api/v1/memory/forget-bulk`` — request contract and wiring (#1941)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from api.routes.memory import forget_bulk
from models.schemas import ForgetBulkRequest

USER = {"user_id": "u1", "api_key_workspace_id": None}
_LAST = uuid4()


class TestTheRequest:
    def test_an_empty_body_is_refused(self) -> None:
        """No filter and no all=true must never wipe a context."""
        with pytest.raises(ValidationError, match="all=true"):
            ForgetBulkRequest(context_id=uuid4())

    def test_an_empty_tag_list_is_not_a_filter(self) -> None:
        with pytest.raises(ValidationError):
            ForgetBulkRequest(context_id=uuid4(), tags=[])

    def test_all_true_is_an_explicit_match_everything(self) -> None:
        req = ForgetBulkRequest(context_id=uuid4(), all=True)
        assert req.dry_run is True

    def test_tags_follow_the_write_caps(self) -> None:
        from models.schemas import MEMORY_TAG_MAX_CHARS, MEMORY_TAGS_MAX_COUNT

        with pytest.raises(ValidationError, match="exceeds"):
            ForgetBulkRequest(context_id=uuid4(), tags=["x" * (MEMORY_TAG_MAX_CHARS + 1)])
        with pytest.raises(ValidationError, match="at most"):
            ForgetBulkRequest(
                context_id=uuid4(), tags=[f"t{i}" for i in range(MEMORY_TAGS_MAX_COUNT + 1)]
            )

    def test_dry_run_is_the_default(self) -> None:
        assert ForgetBulkRequest(context_id=uuid4(), type="note").dry_run is True

    def test_an_inverted_date_range_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="earlier"):
            ForgetBulkRequest(
                context_id=uuid4(),
                created_after=datetime(2026, 2, 1),
                created_before=datetime(2026, 1, 1),
            )


class TestTheRoute:
    @pytest.mark.asyncio
    async def test_dry_run_returns_matched(self) -> None:
        service = MagicMock(forget_bulk=AsyncMock(return_value=(7, False, None)))
        req = ForgetBulkRequest(context_id=uuid4(), type="note")
        resp = await forget_bulk(request=req, user=USER, memory_service=service)
        assert (resp.dry_run, resp.matched, resp.deleted) == (True, 7, None)
        assert service.forget_bulk.await_args.kwargs["dry_run"] is True

    @pytest.mark.asyncio
    async def test_delete_returns_deleted_and_passes_every_filter(self) -> None:
        service = MagicMock(
            forget_bulk=AsyncMock(return_value=(3, True, (datetime(2026, 1, 2, 3, 4, 5), _LAST)))
        )
        ctx = uuid4()
        req = ForgetBulkRequest(
            context_id=ctx,
            created_before=datetime(2026, 1, 1, 9, tzinfo=UTC),
            created_after=datetime(2025, 1, 1),
            type="note",
            tags=["old"],
            dry_run=False,
        )
        resp = await forget_bulk(request=req, user=USER, memory_service=service)
        assert (resp.dry_run, resp.matched, resp.deleted, resp.remaining) == (False, None, 3, True)
        assert resp.next_cursor
        kwargs = service.forget_bulk.await_args.kwargs
        assert kwargs["context_id"] == ctx
        # created_at is naive UTC: an aware bound is normalised.
        assert kwargs["created_before"] == datetime(2026, 1, 1, 9)
        assert kwargs["created_after"] == datetime(2025, 1, 1)
        assert (kwargs["memory_type"], kwargs["tags"]) == ("note", ["old"])
        assert service.forget_bulk.await_args.args == ("u1",)
        assert kwargs["after"] is None
        # The resume point round-trips through the opaque cursor.
        from models.schemas import decode_forget_bulk_cursor

        assert decode_forget_bulk_cursor(resp.next_cursor) == (datetime(2026, 1, 2, 3, 4, 5), _LAST)

    @pytest.mark.asyncio
    async def test_a_cursor_resumes_the_delete(self) -> None:
        from models.schemas import encode_forget_bulk_cursor

        service = MagicMock(forget_bulk=AsyncMock(return_value=(1, False, None)))
        cursor = encode_forget_bulk_cursor(datetime(2026, 1, 2), _LAST)
        req = ForgetBulkRequest(context_id=uuid4(), all=True, dry_run=False, cursor=cursor)
        resp = await forget_bulk(request=req, user=USER, memory_service=service)
        assert service.forget_bulk.await_args.kwargs["after"] == (datetime(2026, 1, 2), _LAST)
        assert resp.next_cursor is None

    @pytest.mark.asyncio
    async def test_a_dry_run_ignores_the_cursor(self) -> None:
        from models.schemas import encode_forget_bulk_cursor

        service = MagicMock(forget_bulk=AsyncMock(return_value=(9, False, None)))
        cursor = encode_forget_bulk_cursor(datetime(2026, 1, 2), _LAST)
        req = ForgetBulkRequest(context_id=uuid4(), all=True, cursor=cursor)
        await forget_bulk(request=req, user=USER, memory_service=service)
        assert service.forget_bulk.await_args.kwargs["after"] is None

    def test_a_malformed_cursor_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="invalid cursor"):
            ForgetBulkRequest(context_id=uuid4(), all=True, cursor="not-a-cursor")
