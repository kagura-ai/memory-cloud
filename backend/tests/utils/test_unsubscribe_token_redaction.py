"""The announcement unsubscribe token never reaches a log line or a table (#2761).

``GET /api/v1/announcement-emails/unsubscribe/<token>`` is opened in the
person's browser, where the ``SameSite=Lax`` session cookie rides along: the
usage middleware then persists the path, and the exception handlers and the
access log record it. The token never expires and names an account, so the
shared scrubber (``redact_invite_tokens``) replaces it like an invite token.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from api.middleware.request_logger import RequestLoggingMiddleware
from utils.url_redact import redact_invite_tokens

# A Fernet token without padding: url-safe base64, a few hundred characters.
TOKEN = "gAAAAABn" + "Zk3v_9Qw-" * 20 + "x1"
PATH = f"/api/v1/announcement-emails/unsubscribe/{TOKEN}"


class TestScrubber:
    def test_path(self) -> None:
        assert redact_invite_tokens(PATH) == "/api/v1/announcement-emails/unsubscribe/{token}"

    def test_full_url_and_header_value(self) -> None:
        header = f"<https://app.example.com{PATH}>"

        assert redact_invite_tokens(header) == (
            "<https://app.example.com/api/v1/announcement-emails/unsubscribe/{token}>"
        )

    def test_access_log_line(self) -> None:
        line = f'203.0.113.7:0 - "POST {PATH} HTTP/1.1" 200'

        out = redact_invite_tokens(line)

        assert TOKEN not in out
        assert "/announcement-emails/unsubscribe/{token} HTTP/1.1" in out

    @pytest.mark.parametrize("short", ["a", "x" * 5, "%0D%0Aevil"])
    def test_any_segment_in_the_slot(self, short: str) -> None:
        out = redact_invite_tokens(f"/api/v1/announcement-emails/unsubscribe/{short}")

        assert out == "/api/v1/announcement-emails/unsubscribe/{token}"

    @pytest.mark.parametrize(
        "text",
        [
            "/api/v1/announcement-emails/unsubscribe",
            "/api/v1/me/announcement-emails",
        ],
    )
    def test_the_other_routes_are_untouched(self, text: str) -> None:
        assert redact_invite_tokens(text) == text


class TestUsageStats:
    @pytest.mark.asyncio
    async def test_signed_in_browser_opening_the_link(self, monkeypatch) -> None:
        log_usage = AsyncMock()
        monkeypatch.setattr("api.middleware.request_logger.log_usage", log_usage)

        async def fake_get_db():
            yield MagicMock(close=AsyncMock())

        monkeypatch.setattr("api.middleware.request_logger.get_db", fake_get_db)

        request = MagicMock()
        request.url.path = PATH
        request.method = "GET"
        request.state = MagicMock(user_id="user_1", workspace_id=None)

        middleware = RequestLoggingMiddleware(app=MagicMock())
        await middleware.dispatch(request, AsyncMock(return_value=MagicMock(status_code=303)))

        assert (
            log_usage.await_args.kwargs["endpoint"]
            == "/api/v1/announcement-emails/unsubscribe/{token}"
        )
        assert TOKEN not in repr(log_usage.await_args)
