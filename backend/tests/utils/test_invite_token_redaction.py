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

Workspace invitation tokens (``secrets.token_urlsafe(32)``, lifetime up to a
year or unlimited) travel the same way and reach the same sinks: the public
preview ``GET /api/v1/invitations/{token}``, the landing URL
``{FRONTEND_URL}/invite/{token}`` and its percent-encoded copy in the OAuth
login's ``return_to``. The same scrubber covers those three shapes too.
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

    def test_workspace_invitation_preview_path(self) -> None:
        """GET /api/v1/invitations/{token} — the unauthenticated invite preview."""
        assert redact_invite_tokens(f"/api/v1/invitations/{TOKEN}") == "/api/v1/invitations/{token}"
        line = f'203.0.113.7:5123 - "GET /api/v1/invitations/{TOKEN} HTTP/1.1" 200'
        assert redact_invite_tokens(line) == (
            '203.0.113.7:5123 - "GET /api/v1/invitations/{token} HTTP/1.1" 200'
        )

    def test_frontend_invite_url(self) -> None:
        """{FRONTEND_URL}/invite/{token} is the credential — and the callback's Location."""
        assert (
            redact_invite_tokens(f"https://app.example.test/invite/{TOKEN}")
            == "https://app.example.test/invite/{token}"
        )
        assert redact_invite_tokens(f"/invite/{TOKEN}?_rsc=1a2b3") == "/invite/{token}?_rsc=1a2b3"

    @pytest.mark.parametrize("slash", ["%2F", "%2f"])
    def test_percent_encoded_return_to(self, slash: str) -> None:
        """The invite page sends its own URL percent-encoded in the login's return_to."""
        path = (
            f"/api/v1/auth/google/login?return_to=https%3A{slash}{slash}app.example.test"
            f"{slash}invite{slash}{TOKEN}&accepted_terms=2026-01"
        )
        out = redact_invite_tokens(path)
        assert TOKEN not in out
        assert out == (
            f"/api/v1/auth/google/login?return_to=https%3A{slash}{slash}app.example.test"
            f"{slash}invite{slash}{{token}}&accepted_terms=2026-01"
        )

    @pytest.mark.parametrize(
        "text",
        [
            "/api/v1/beta-invites/me",
            "/api/v1/beta-invites/0b9f6c1e-5d0a-4a3e-9d57-2f4f3f6f8a11",
            # The token-free invitation routes keep their concrete path.
            "/api/v1/invitations/accept",
            "/api/v1/invitations/pending",
            # Managing an invitation goes by its id, under the workspace.
            "/api/v1/workspaces/0b9f6c1e-5d0a-4a3e-9d57-2f4f3f6f8a11"
            "/invitations/7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f",
            "/api/v1/contexts/0b9f6c1e-5d0a-4a3e-9d57-2f4f3f6f8a11/join/short",
            "/invite/short",
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

    def test_database_error_path_is_scrubbed(self) -> None:
        """The ``database_error`` handler logs the path of every request that hit
        a database outage — the invite preview included."""
        event = {
            "event": "database_error",
            "error_code": "DB-001",
            "error_type": "OperationalError",
            "path": f"/api/v1/invitations/{TOKEN}",
        }
        out = redact_pg_detail(None, "error", event)
        assert out["path"] == "/api/v1/invitations/{token}"
        assert out["error_type"] == "OperationalError"

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

    def test_session_middleware_debug_line_is_scrubbed(
        self, monkeypatch, capsys, _logging_reset
    ) -> None:
        """``SessionMiddleware`` logs ``No session cookie: <path>`` at DEBUG for
        every anonymous request — the invite preview's path included."""
        monkeypatch.setenv("LOG_COLORIZE", "false")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        root, _access = _logging_reset
        root.handlers.clear()
        setup_logger(enable_colors=False)

        logging.getLogger(f"stdlib-invite-{uuid.uuid4().hex}").debug(
            f"No session cookie: /api/v1/invitations/{TOKEN}"
        )

        out = capsys.readouterr().out
        assert "No session cookie: /api/v1/invitations/{token}" in out
        assert TOKEN not in out


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
    async def test_signed_in_invitee_opening_the_invite_page(self, monkeypatch) -> None:
        """The /invite/{token} page calls the preview with whatever session the
        browser has. An invitee who is already signed in (or an inviter checking
        their own link) gets a usage row — it must carry the route, not the token."""
        log_usage = AsyncMock()
        monkeypatch.setattr("api.middleware.request_logger.log_usage", log_usage)

        async def fake_get_db():
            yield MagicMock(close=AsyncMock())

        monkeypatch.setattr("api.middleware.request_logger.get_db", fake_get_db)

        request = MagicMock()
        request.url.path = f"/api/v1/invitations/{TOKEN}"
        request.method = "GET"
        request.state = MagicMock(user_id="user_1", workspace_id=None)
        response = MagicMock(status_code=200)

        middleware = RequestLoggingMiddleware(app=MagicMock())
        await middleware.dispatch(request, AsyncMock(return_value=response))

        assert log_usage.await_args.kwargs["endpoint"] == "/api/v1/invitations/{token}"
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
