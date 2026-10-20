"""The consent page's default-context picker (#2129).

GET ``/oauth/authorize`` renders a ``<select name="default_context">`` —
"No default" first, then the user's readable contexts grouped by workspace,
the grant's stored choice preselected — tied to the approve form. POST reads
the field before the code is issued: empty clears, a readable UUID is stored,
anything else is ignored without interrupting the authorization.
"""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routes import oauth as oauth_routes

_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
CTX_A, CTX_B = uuid.uuid4(), uuid.uuid4()


def _render(groups, selected: str = "") -> tuple[int, str]:
    from models.auth import OAuth2Client

    fake_user = MagicMock(email="test@example.com", user_id="user-1")
    fake_client = OAuth2Client(
        client_id="test",
        client_name="Some App",
        client_secret_hash="x",
        redirect_uris=["https://app.example/cb"],
        scope="memory:read memory:write",
        token_endpoint_auth_method="none",
        grant_types=["authorization_code"],
        response_types=["code"],
    )
    choices = AsyncMock(return_value=(groups, selected))
    with (
        patch("api.routes.oauth.get_current_user_from_session", return_value=fake_user),
        patch("api.routes.oauth.get_sync_session") as mock_sess,
        patch("api.routes.oauth._consent_default_context_choices", new=choices),
    ):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.side_effect = [
            fake_client,
            MagicMock(locale="en"),
        ]
        mock_sess.return_value = db
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/api/v1/oauth/authorize",
                params={
                    "response_type": "code",
                    "client_id": "test",
                    "redirect_uri": "https://app.example/cb",
                    "state": "s",
                    "code_challenge": _CHALLENGE,
                    "code_challenge_method": "S256",
                },
                follow_redirects=False,
            )
    choices.assert_awaited_once_with("user-1", "test")
    return response.status_code, response.text


GROUPS = [
    {"workspace": "Team <One>", "contexts": [{"id": str(CTX_A), "name": "alpha & co"}]},
    {"workspace": "Personal", "contexts": [{"id": str(CTX_B), "name": "beta"}]},
]


def test_the_picker_lists_no_default_first_then_contexts_by_workspace():
    status, html = _render(GROUPS)

    assert status == 200
    assert '<select id="default-context" name="default_context" form="approve-form">' in html
    assert '<form id="approve-form" method="POST"' in html
    no_default = html.index('<option value="" selected>No default</option>')
    first_group = html.index('<optgroup label="Team &lt;One&gt;">')
    assert no_default < first_group < html.index('<optgroup label="Personal">')
    # Names are escaped like every other value on the page.
    assert f'<option value="{CTX_A}">alpha &amp; co</option>' in html


def test_the_stored_choice_is_preselected():
    _, html = _render(GROUPS, selected=str(CTX_B))

    assert f'<option value="{CTX_B}" selected>beta</option>' in html
    assert '<option value="">No default</option>' in html


def test_a_stored_default_the_picker_does_not_list_is_kept_by_default():
    _, html = _render(GROUPS, selected="keep")

    assert '<option value="keep" selected>Keep the current default</option>' in html
    assert '<option value="">No default</option>' in html


def test_without_contexts_the_page_has_no_picker():
    status, html = _render([])

    assert status == 200
    assert 'name="default_context"' not in html


def test_the_picker_strings_exist_in_both_locales():
    from utils.oauth_messages import OAUTH_MESSAGES

    for locale in ("en", "ja"):
        for key in (
            "default_context_label",
            "default_context_none",
            "default_context_keep",
            "default_context_hint",
        ):
            assert OAUTH_MESSAGES[locale][key]


# ------------------------------------------------------------------ POST


def _post(
    form: dict[str, str], location: str = "https://app.example/cb?code=abc&state=s"
) -> tuple[int, AsyncMock]:
    fake_user = MagicMock(email="test@example.com", user_id="user-1")
    fake_client = MagicMock(client_name="App", scope="memory:read")
    fake_client.check_redirect_uri = MagicMock(return_value=True)
    save = AsyncMock()
    redirect = SimpleNamespace(location=location)
    with (
        patch("api.routes.oauth.get_current_user_from_session", return_value=fake_user),
        patch("api.routes.oauth.get_sync_session") as mock_sess,
        patch("api.routes.oauth._consent_is_new", return_value=False),
        patch("api.routes.oauth._run_oauth_sync", return_value=redirect),
        patch("api.routes.oauth._save_consent_default_context", new=save),
    ):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = fake_client
        mock_sess.return_value = db
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/api/v1/oauth/authorize",
                params={"client_id": "test", "redirect_uri": "https://app.example/cb"},
                data=form,
                follow_redirects=False,
            )
    return response.status_code, save


def test_approve_stores_the_picked_context_once_the_code_is_issued():
    status, save = _post({"confirm": "yes", "default_context": str(CTX_A)})

    assert status == 303
    save.assert_awaited_once_with("user-1", "test", str(CTX_A))


def test_a_refused_request_stores_nothing():
    """Authlib answered with an error redirect: no grant, no setting change."""
    _, save = _post(
        {"confirm": "yes", "default_context": ""},
        location="https://app.example/cb?error=invalid_request&state=s",
    )

    save.assert_not_awaited()


def test_the_picker_is_not_read_for_a_request_that_fails_validation():
    fake_user = MagicMock(email="test@example.com", user_id="user-1")
    fake_client = MagicMock(client_name="App", scope="memory:read")
    fake_client.check_redirect_uri = MagicMock(return_value=False)
    choices = AsyncMock(return_value=([], ""))
    with (
        patch("api.routes.oauth.get_current_user_from_session", return_value=fake_user),
        patch("api.routes.oauth.get_sync_session") as mock_sess,
        patch("api.routes.oauth._consent_default_context_choices", new=choices),
    ):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = fake_client
        mock_sess.return_value = db
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/api/v1/oauth/authorize",
                params={"client_id": "test", "redirect_uri": "https://evil.example/cb"},
                follow_redirects=False,
            )

    assert response.status_code == 400
    choices.assert_not_awaited()


def test_deny_stores_nothing():
    _, save = _post({"confirm": "no", "default_context": str(CTX_A)})

    save.assert_not_awaited()


# ------------------------------------------------------------------ the save step


@contextlib.contextmanager
def _save_env(readable):
    db = MagicMock(commit=AsyncMock())

    async def get_db():
        yield db

    setter = AsyncMock()
    with (
        patch("db.base.get_db", get_db),
        patch(
            "services.credential_default_context.readable_context",
            new=AsyncMock(return_value=readable),
        ) as reader,
        patch("services.credential_default_context.set_grant_default_context", new=setter),
    ):
        yield SimpleNamespace(db=db, setter=setter, reader=reader)


@pytest.mark.asyncio
async def test_a_readable_uuid_is_stored():
    with _save_env(SimpleNamespace(name="alpha")) as env:
        await oauth_routes._save_consent_default_context("user-1", "client-1", f" {CTX_A} ")

    env.setter.assert_awaited_once_with(
        env.db, user_id="user-1", client_id="client-1", context_id=CTX_A
    )
    assert env.reader.await_args.kwargs == {"user_id": "user-1", "context_id": CTX_A}
    env.db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_empty_value_clears():
    with _save_env(None) as env:
        await oauth_routes._save_consent_default_context("user-1", "client-1", "")

    env.setter.assert_awaited_once_with(
        env.db, user_id="user-1", client_id="client-1", context_id=None
    )
    env.reader.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [None, "my-project", "keep"])
async def test_an_absent_malformed_or_keep_value_is_ignored(raw):
    with _save_env(SimpleNamespace(name="alpha")) as env:
        await oauth_routes._save_consent_default_context("user-1", "client-1", raw)

    env.setter.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unreadable_context_is_ignored():
    with _save_env(None) as env:
        await oauth_routes._save_consent_default_context("user-1", "client-1", str(CTX_A))

    env.setter.assert_not_awaited()
    env.db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failure_never_interrupts_the_authorization():
    async def broken_get_db():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    with patch("db.base.get_db", broken_get_db):
        await oauth_routes._save_consent_default_context("user-1", "client-1", str(CTX_A))


@pytest.mark.asyncio
async def test_the_choices_group_by_workspace_and_keep_a_listed_selection():
    from services.credential_default_context import ContextOption

    options = [
        ContextOption(CTX_A, "alpha", "Team"),
        ContextOption(CTX_B, "beta", "Team"),
        ContextOption(uuid.uuid4(), "gamma", "Personal"),
    ]

    async def get_db():
        yield MagicMock()

    with (
        patch("db.base.get_db", get_db),
        patch(
            "services.credential_default_context.readable_context_options",
            new=AsyncMock(return_value=options),
        ),
        patch(
            "services.credential_default_context.grant_default_context_id",
            new=AsyncMock(return_value=CTX_B),
        ),
    ):
        groups, selected = await oauth_routes._consent_default_context_choices("u", "c")

    assert [g["workspace"] for g in groups] == ["Team", "Personal"]
    assert [c["name"] for c in groups[0]["contexts"]] == ["alpha", "beta"]
    assert selected == str(CTX_B)


@pytest.mark.asyncio
async def test_a_stored_choice_no_longer_listed_is_not_selected():
    async def get_db():
        yield MagicMock()

    with (
        patch("db.base.get_db", get_db),
        patch(
            "services.credential_default_context.readable_context_options",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "services.credential_default_context.grant_default_context_id",
            new=AsyncMock(return_value=CTX_B),
        ),
    ):
        assert await oauth_routes._consent_default_context_choices("u", "c") == ([], "keep")


@pytest.mark.asyncio
async def test_a_mock_user_id_reads_nothing():
    """A non-string user id (a test double) never opens a session."""
    with patch("db.base.get_db", side_effect=AssertionError("opened a session")):
        assert await oauth_routes._consent_default_context_choices(MagicMock(), "c") == ([], "")


def test_the_loopback_interstitial_path_stores_the_choice_too():
    """#2155: a loopback client gets an interstitial instead of the 303; the
    default is stored on that path as well, once the code is issued."""
    from fastapi.responses import HTMLResponse

    fake_user = MagicMock(email="test@example.com", user_id="user-1")
    fake_client = MagicMock(client_name="CLI", scope="memory:read")
    fake_client.check_redirect_uri = MagicMock(return_value=True)
    save = AsyncMock()
    redirect = SimpleNamespace(location="http://127.0.0.1:8765/cb?code=abc&state=s")
    with (
        patch("api.routes.oauth.get_current_user_from_session", return_value=fake_user),
        patch("api.routes.oauth.get_sync_session") as mock_sess,
        patch("api.routes.oauth._consent_is_new", return_value=False),
        patch("api.routes.oauth._run_oauth_sync", return_value=redirect),
        patch("api.routes.oauth._save_consent_default_context", new=save),
        patch(
            "api.routes.oauth._render_loopback_interstitial",
            return_value=HTMLResponse("interstitial"),
        ),
    ):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = fake_client
        mock_sess.return_value = db
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/api/v1/oauth/authorize",
                params={"client_id": "test", "redirect_uri": "http://127.0.0.1:8765/cb"},
                data={"confirm": "yes", "default_context": str(CTX_A)},
                follow_redirects=False,
            )

    assert response.status_code == 200
    assert response.text == "interstitial"
    save.assert_awaited_once_with("user-1", "test", str(CTX_A))
