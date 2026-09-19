"""A closed-beta invite token must never reach a log line or a table (#1581).

The API contract puts the plaintext token in URLs — the public preview's PATH
(``/beta-invites/{token}/preview``) and the OAuth login's QUERY
(``?invite=<token>``). Before this guard, those URLs were recorded verbatim by:

- the global exception handlers (``path=request.url.path`` on every
  404 / 410 / 429 — a rate-limited preview of a VALID link logged a live token),
- uvicorn's access log (the whole request line, path + query),
- ``RequestLoggingMiddleware``, which PERSISTS the path to ``usage_stats`` for
  any signed-in caller — a plaintext token at rest in the database.

The fix lives at the existing chokepoints (the #1359 pattern): one scrubber,
applied by the structlog processor, the stdlib formatter wrapper (now also over
uvicorn's own handlers), and the usage middleware.
"""

from __future__ import annotations

import io
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from api.middleware.request_logger import RequestLoggingMiddleware
from utils.logger import redact_pg_detail, setup_logger
from utils.url_redact import redact_invite_tokens

TOKEN = "Zk3v_9Qw-" + "a" * 34  # 43 chars, token_urlsafe alphabet


class TestRedactInviteTokens:
    def test_preview_path(self) -> None:
        assert (
            redact_invite_tokens(f"/api/v1/beta-invites/{TOKEN}/preview")
            == "/api/v1/beta-invites/{token}/preview"
        )

    def test_login_query_in_any_position(self) -> None:
        first = redact_invite_tokens(f"/api/v1/auth/google/login?invite={TOKEN}&return_to=/")
        last = redact_invite_tokens(f"/api/v1/auth/github/login?return_to=%2F&invite={TOKEN}")
        assert first == "/api/v1/auth/google/login?invite={token}&return_to=/"
        assert last == "/api/v1/auth/github/login?return_to=%2F&invite={token}"

    def test_frontend_join_url(self) -> None:
        """The landing URL is the credential itself — e.g. inside a return_to."""
        assert (
            redact_invite_tokens(f"https://app.example.test/join/{TOKEN}")
            == "https://app.example.test/join/{token}"
        )

    def test_uvicorn_access_log_line(self) -> None:
        line = f'203.0.113.7:5123 - "GET /api/v1/beta-invites/{TOKEN}/preview HTTP/1.1" 200'
        out = redact_invite_tokens(line)
        assert TOKEN not in out
        assert out.endswith('/preview HTTP/1.1" 200')

    @pytest.mark.parametrize(
        "text",
        [
            "/api/v1/beta-invites/me",
            "/api/v1/beta-invites/0b9f6c1e-5d0a-4a3e-9d57-2f4f3f6f8a11",
            "/api/v1/invitations/some-workspace-token",
            "/api/v1/contexts/0b9f6c1e-5d0a-4a3e-9d57-2f4f3f6f8a11/join/short",
            "/api/v1/resources/res-1/events",
            "plain message with no url at all",
        ],
    )
    def test_everything_else_is_untouched(self, text: str) -> None:
        assert redact_invite_tokens(text) == text


class TestStructlogProcessor:
    def test_path_field_is_scrubbed(self) -> None:
        """The exact shape the global MemoryCloudException handler emits."""
        event = {
            "event": "exception_occurred",
            "error": "RATE-001",
            "status_code": 429,
            "path": f"/api/v1/beta-invites/{TOKEN}/preview",
        }
        out = redact_pg_detail(None, "error", event)
        assert out["path"] == "/api/v1/beta-invites/{token}/preview"
        assert out["status_code"] == 429

    def test_nested_and_fstring_values_are_scrubbed(self) -> None:
        event = {
            "event": f"OAuth2 callback failed: GET /login?invite={TOKEN}",
            "request": {"url": f"https://app.example.test/join/{TOKEN}", "n": 1},
            "history": [f"/api/v1/beta-invites/{TOKEN}/preview"],
        }
        out = redact_pg_detail(None, "error", event)
        assert TOKEN not in repr(out)
        assert out["request"]["n"] == 1

    def test_detail_redaction_still_works_alongside(self) -> None:
        event = {"error": 'violates check "c"\nDETAIL:  Failing row contains (secret).'}
        out = redact_pg_detail(None, "error", event)
        assert "secret" not in out["error"]
        assert "DETAIL: [redacted]" in out["error"]


@pytest.fixture
def _logging_reset():
    saved_structlog = structlog.get_config()
    root = logging.getLogger()
    access = logging.getLogger("uvicorn.access")
    saved = (root.handlers[:], access.handlers[:], access.propagate)
    yield root, access
    root.handlers[:], access.handlers[:], access.propagate = saved
    structlog.configure(**saved_structlog)


class TestRenderedPipelines:
    """Through the real ``setup_logger()`` config — what production prints."""

    @pytest.mark.parametrize("colors", [False, True])
    def test_exception_handler_event_renders_without_the_token(
        self, monkeypatch, capsys, _logging_reset, colors: bool
    ) -> None:
        monkeypatch.setenv("LOG_COLORIZE", "true" if colors else "false")
        setup_logger(enable_colors=colors)

        structlog.get_logger(f"invite-redaction-{uuid.uuid4().hex}").error(
            "exception_occurred",
            error="RATE-001",
            status_code=429,
            path=f"/api/v1/beta-invites/{TOKEN}/preview",
        )

        out = capsys.readouterr().out
        assert "exception_occurred" in out
        assert TOKEN not in out
        assert "/beta-invites/{token}/preview" in out


class TestStdlibAndUvicorn:
    def test_uvicorn_access_handler_is_wrapped(self, monkeypatch, _logging_reset) -> None:
        """uvicorn.access has its own non-propagating handler, so wrapping the
        root handlers alone would leave every request line unredacted."""
        monkeypatch.setenv("LOG_COLORIZE", "false")
        _root, access = _logging_reset
        sink = io.StringIO()
        handler = logging.StreamHandler(sink)
        handler.setFormatter(logging.Formatter("ACCESS %(message)s"))
        access.handlers[:] = [handler]
        access.propagate = False
        access.setLevel(logging.INFO)

        setup_logger(enable_colors=False)
        setup_logger(enable_colors=False)  # idempotent: no double wrap

        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "203.0.113.7:5123",
            "GET",
            f"/api/v1/auth/google/login?return_to=%2F&invite={TOKEN}",
            "1.1",
            303,
        )
        out = sink.getvalue()
        assert out.startswith("ACCESS ")  # host format preserved
        assert TOKEN not in out
        assert "invite={token}" in out

    def test_plain_stdlib_logger_is_scrubbed(self, monkeypatch, capsys, _logging_reset) -> None:
        monkeypatch.setenv("LOG_COLORIZE", "false")
        root, _access = _logging_reset
        root.handlers.clear()
        setup_logger(enable_colors=False)

        logging.getLogger(f"stdlib-invite-{uuid.uuid4().hex}").warning(
            "No session cookie: /api/v1/beta-invites/%s/preview", TOKEN
        )

        assert TOKEN not in capsys.readouterr().out


class TestUsageStatsNeverStoresTheToken:
    @pytest.mark.asyncio
    async def test_signed_in_caller_previewing_a_link(self, monkeypatch) -> None:
        """A signed-in person opening a /join link (the UI's already_signed_in
        state, or an inviter checking their own link) hits the preview WITH a
        session. The usage row must carry the route shape, not the token."""
        log_usage = AsyncMock()
        monkeypatch.setattr("api.middleware.request_logger.log_usage", log_usage)

        async def fake_get_db():
            yield MagicMock(close=AsyncMock())

        monkeypatch.setattr("api.middleware.request_logger.get_db", fake_get_db)

        request = MagicMock()
        request.url.path = f"/api/v1/beta-invites/{TOKEN}/preview"
        request.method = "GET"
        request.state = MagicMock(user_id="user_1", workspace_id=None)
        response = MagicMock(status_code=200)

        middleware = RequestLoggingMiddleware(app=MagicMock())
        await middleware.dispatch(request, AsyncMock(return_value=response))

        endpoint = log_usage.await_args.kwargs["endpoint"]
        assert endpoint == "/api/v1/beta-invites/{token}/preview"
        assert TOKEN not in repr(log_usage.await_args)

    @pytest.mark.asyncio
    async def test_other_endpoints_are_recorded_verbatim(self, monkeypatch) -> None:
        """usage_stats readers LIKE-match concrete paths (resources/%/events,
        public/%/search) — the redaction must not turn paths into templates."""
        log_usage = AsyncMock()
        monkeypatch.setattr("api.middleware.request_logger.log_usage", log_usage)

        async def fake_get_db():
            yield MagicMock(close=AsyncMock())

        monkeypatch.setattr("api.middleware.request_logger.get_db", fake_get_db)

        request = MagicMock()
        request.url.path = "/api/v1/resources/res-1/events"
        request.method = "POST"
        request.state = MagicMock(user_id="user_1", workspace_id=None)

        middleware = RequestLoggingMiddleware(app=MagicMock())
        await middleware.dispatch(request, AsyncMock(return_value=MagicMock(status_code=201)))

        assert log_usage.await_args.kwargs["endpoint"] == "/api/v1/resources/res-1/events"
