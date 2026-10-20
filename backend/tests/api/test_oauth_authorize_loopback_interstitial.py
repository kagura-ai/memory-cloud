"""Issue #2155: the OAuth loopback interstitial.

A native MCP client (Claude Code, Cursor, …) registers a loopback redirect
such as ``http://localhost:<port>/callback``. When the browser cannot reach
that listener (WSL2 NAT, SSH, a second device), the bare 303 after consent
ended in a connection error and the authorization code was lost.

``POST /authorize`` now answers an approved loopback request with a page that
shows the full callback URL and a link performing the redirect. These tests
pin that page and that every other branch is unchanged:

1. Loopback approve (``localhost`` and ``127.0.0.1``) → 200 HTML with the
   callback URL once in the link and once in the read-only field, no script,
   and the no-store / no-referrer / noindex / CSP headers.
2. Request-derived values (client name, callback URL) are HTML-escaped.
3. Non-loopback approve, loopback deny and a loopback error redirect still
   answer with the same 303 as before.
4. The #1752 security notification is still scheduled on a first consent.
5. The code reaches no log line, and is never part of a URL on our side.
6. English and Japanese messages.
"""

import html
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402
from api.routes import oauth as oauth_routes  # noqa: E402
from utils.oauth_messages import OAUTH_MESSAGES  # noqa: E402

CODE = "Zq9secretAuthCode7"


def _fake_client(client_name: str = "Claude Code"):
    fake = MagicMock(
        client_name=client_name,
        scope="memory:read memory:write",
        token_endpoint_auth_method="none",
    )
    fake.check_redirect_uri = MagicMock(return_value=True)
    return fake


def _post(
    redirect_uri: str,
    *,
    location: str | None = None,
    confirm: str = "yes",
    locale: str = "en",
    client_name: str = "Claude Code",
    first: bool = False,
    client_row: object = "default",
):
    """POST /authorize with the session, DB and Authlib grant mocked.

    Returns ``(response, schedule_mock)``.
    """
    if location is None:
        location = f"{redirect_uri}?code={CODE}&state=st-1"
    fake_user = oauth_routes._OAuthUser(user_id="u-2155", email="u2155@example.test")
    row = _fake_client(client_name) if client_row == "default" else client_row

    # The pre-check reads the client first; the interstitial lookup then gets
    # ``client_row`` (None simulates a client deleted in between).
    client_rows = iter([_fake_client(client_name)])

    def _query(model):
        q = MagicMock()
        if model is oauth_routes.OAuth2Client:
            q.filter_by.return_value.first.side_effect = lambda: next(client_rows, row)
        else:
            q.filter_by.return_value.first.return_value = None
        return q

    with (
        patch("api.routes.oauth.get_current_user_from_session", return_value=fake_user),
        patch("api.routes.oauth.get_sync_session") as mock_sess,
        patch("api.routes.oauth._consent_is_new", return_value=first),
        patch(
            "api.routes.oauth._run_oauth_sync",
            return_value=SimpleNamespace(location=location),
        ),
        patch("api.routes.oauth.schedule_security_notification") as schedule,
    ):
        db = MagicMock()
        db.query.side_effect = _query
        mock_sess.return_value = db
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/api/v1/oauth/authorize",
                params={
                    "client_id": "cc-client",
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "state": "st-1",
                    "locale": locale,
                },
                data={"confirm": confirm},
                follow_redirects=False,
            )
    return response, schedule


class TestLoopbackInterstitial:
    @pytest.mark.parametrize(
        "redirect_uri",
        ["http://localhost:4321/callback", "http://127.0.0.1:4321/callback"],
    )
    def test_loopback_approve_renders_the_callback_url(self, redirect_uri):
        response, _ = _post(redirect_uri)

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "location" not in response.headers
        location = f"{redirect_uri}?code={CODE}&state=st-1"
        escaped = html.escape(location, quote=True)
        body = response.text
        # Once in the link (works with JS off), once in the read-only field.
        assert body.count(escaped) == 2
        assert f'href="{escaped}"' in body
        assert f">{escaped}</textarea>" in body
        assert body.count(CODE) == 2
        assert "<script" not in body.lower()

    def test_interstitial_headers(self):
        response, _ = _post("http://localhost:4321/callback")

        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-robots-tag"] == "noindex"
        csp = response.headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "script-src" not in csp  # no script runs on this page

    def test_request_values_are_escaped(self):
        hostile = 'http://localhost:4321/callback?code=x&state=<b>"q'
        response, _ = _post(
            "http://localhost:4321/callback",
            location=hostile,
            client_name="<img src=x onerror=alert(1)>",
        )

        assert response.status_code == 200
        body = response.text
        assert "<b>" not in body
        assert "<img src=x" not in body
        assert "&lt;b&gt;&#34;q" in body or "&lt;b&gt;&quot;q" in body
        assert "&lt;img src=x onerror=alert(1)&gt;" in body

    def test_english_messages(self):
        response, _ = _post("http://localhost:4321/callback", locale="en")
        body = response.text
        en = OAUTH_MESSAGES["en"]

        assert '<html lang="en">' in body
        assert "Return to Claude Code" in body
        assert "Continue to Claude Code" in body
        assert str(en["loopback_url_label"]) in body
        assert "/mcp" in body

    def test_japanese_messages(self):
        response, _ = _post("http://localhost:4321/callback", locale="ja")
        body = response.text
        ja = OAUTH_MESSAGES["ja"]

        assert '<html lang="ja">' in body
        assert "Claude Code に戻る" in body
        assert "Claude Code に進む" in body
        assert str(ja["loopback_url_label"]) in body

    def test_vanished_client_falls_back_to_the_redirect(self):
        response, _ = _post("http://localhost:4321/callback", client_row=None)

        assert response.status_code == 303
        assert response.headers["location"] == (
            f"http://localhost:4321/callback?code={CODE}&state=st-1"
        )

    def test_render_error_falls_back_to_the_redirect(self):
        with patch.object(
            oauth_routes.templates, "TemplateResponse", side_effect=RuntimeError("boom")
        ):
            response, _ = _post("http://localhost:4321/callback")

        assert response.status_code == 303
        assert response.headers["location"] == (
            f"http://localhost:4321/callback?code={CODE}&state=st-1"
        )


class TestOtherBranchesUnchanged:
    def test_non_loopback_approve_is_the_same_303(self):
        response, _ = _post("https://example.com/cb")

        assert response.status_code == 303
        assert response.headers["location"] == f"https://example.com/cb?code={CODE}&state=st-1"
        assert response.text == ""

    def test_loopback_deny_is_the_same_303(self):
        response, _ = _post("http://localhost:4321/callback", confirm="no")

        assert response.status_code == 303
        assert response.headers["location"] == (
            "http://localhost:4321/callback?error=access_denied&state=st-1"
        )

    def test_loopback_error_redirect_is_not_an_interstitial(self):
        location = "http://localhost:4321/callback?error=invalid_scope&state=st-1"
        response, _ = _post("http://localhost:4321/callback", location=location)

        assert response.status_code == 303
        assert response.headers["location"] == location


class TestNotificationAndLogging:
    def test_first_loopback_consent_still_notifies(self):
        response, schedule = _post("http://localhost:4321/callback", first=True)

        assert response.status_code == 200
        schedule.assert_called_once()
        assert schedule.call_args.kwargs["client_id"] == "cc-client"
        assert CODE not in repr(schedule.call_args)

    def test_repeat_loopback_consent_does_not_notify(self):
        response, schedule = _post("http://localhost:4321/callback", first=False)

        assert response.status_code == 200
        schedule.assert_not_called()

    def test_code_reaches_no_log_line(self, caplog, capsys):
        caplog.set_level(logging.DEBUG)
        with capture_logs() as structured:
            response, _ = _post("http://localhost:4321/callback", first=True)

        assert response.status_code == 200
        assert CODE not in caplog.text
        assert all(CODE not in repr(event) for event in structured)
        captured = capsys.readouterr()
        assert CODE not in captured.out
        assert CODE not in captured.err

    def test_code_is_never_in_a_request_url_on_our_side(self):
        response, _ = _post("http://localhost:4321/callback")

        # The code lives in the body of the POST response; the request the
        # access log records carries no code.
        assert "code=" not in str(response.request.url)
        assert CODE in response.text
