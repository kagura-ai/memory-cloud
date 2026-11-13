"""The Google callback's blocking calls run off the event loop (#2501).

``exchange_code_web``, ``get_user_info_web`` and ``verified_auth_time`` block on
HTTP to Google (``requests``); called from the ``async def`` handler they would
stall every other request on the worker. They run in the bounded
``_GOOGLE_OAUTH_EXECUTOR``, not on the loop and not on asyncio's default pool
(the head-room the OAuth token endpoint draws on, #1885).
"""

from __future__ import annotations

import ast
import contextvars
import inspect
import os
import textwrap
import threading
from unittest.mock import MagicMock, patch

import pytest
from fastapi.responses import RedirectResponse

from api.routes import auth as auth_module
from api.routes.auth import _run_google_sync, google_callback
from tests.oauth_browser_helpers import assume_bound_browser  # noqa: F401

POOL_PREFIX = "google-oauth"


@pytest.mark.usefixtures("assume_bound_browser")
async def test_google_exchange_and_userinfo_run_in_the_google_pool() -> None:
    threads: dict[str, str] = {}
    credentials = object()

    def fake_exchange(code: str, redirect_uri: str) -> object:
        threads["exchange"] = threading.current_thread().name
        assert (code, redirect_uri) == ("c", "http://localhost:8080/cb")
        return credentials

    def fake_userinfo(creds: object) -> dict[str, str]:
        threads["userinfo"] = threading.current_thread().name
        assert creds is credentials
        return {"sub": "108", "email": "u@example.test"}

    session_mgr = MagicMock()
    session_mgr._redis.get.return_value = "pending"
    oauth_mgr = MagicMock()
    oauth_mgr.exchange_code_web.side_effect = fake_exchange
    oauth_mgr.get_user_info_web.side_effect = fake_userinfo
    # Stop the flow right after the two calls: the link short-circuit is the
    # first step that consumes user_info.
    stop = RedirectResponse("http://localhost:3000/settings", status_code=303)

    async def fake_link_redirect(**kwargs: object) -> RedirectResponse:
        assert kwargs["idp_sub"] == "108"
        return stop

    request = MagicMock()
    request.client.host = "127.0.0.1"
    request.headers = {}
    with (
        patch.object(auth_module, "_session_manager", session_mgr),
        patch.object(auth_module, "_oauth2_manager", oauth_mgr),
        patch.object(auth_module, "_maybe_link_redirect", fake_link_redirect),
        patch.dict(os.environ, {"GOOGLE_REDIRECT_URI": "http://localhost:8080/cb"}),
    ):
        resp = await google_callback(
            request, code="c", state="s", error=None, error_description=None
        )

    assert resp is stop
    assert threads["exchange"].startswith(POOL_PREFIX)
    assert threads["userinfo"].startswith(POOL_PREFIX)


_REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")


async def test_run_google_sync_copies_contextvars_and_reraises() -> None:
    _REQUEST_ID.set("req-1")
    assert await _run_google_sync(lambda suffix: _REQUEST_ID.get() + suffix, "!") == "req-1!"

    def boom() -> None:
        raise ValueError("from the worker")

    with pytest.raises(ValueError, match="from the worker"):
        await _run_google_sync(boom)


def test_google_callback_never_calls_the_oauth2_manager_directly() -> None:
    """Every ``_oauth2_manager`` method is handed to ``_run_google_sync``.

    A direct ``_oauth2_manager.x(...)`` call in the handler would run on the
    event loop; all of its methods on this path block on HTTP.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(google_callback)))
    direct = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "_oauth2_manager"
    ]
    assert direct == []
    handed = [
        node.args[0].attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_run_google_sync"
        and isinstance(node.args[0], ast.Attribute)
    ]
    assert sorted(handed) == ["exchange_code_web", "get_user_info_web", "verified_auth_time"]
