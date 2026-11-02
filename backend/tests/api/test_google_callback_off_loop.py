"""The Google callback's blocking HTTP calls run off the event loop (#2501).

``exchange_code_web`` and ``get_user_info_web`` use ``requests``; called
directly from the ``async def`` handler they stall every other request on the
worker while Google answers. Each fake below records whether it ran with an
event loop in its thread: inside ``asyncio.to_thread`` there is none.
"""

from __future__ import annotations

import asyncio
import os
import threading
from unittest.mock import MagicMock, patch

from fastapi.responses import RedirectResponse

from api.routes import auth as auth_module
from api.routes.auth import google_callback


def _ran_on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


async def test_google_exchange_and_userinfo_run_off_the_event_loop() -> None:
    seen: dict[str, tuple[bool, int]] = {}
    credentials = object()

    def fake_exchange(code: str, redirect_uri: str) -> object:
        seen["exchange"] = (_ran_on_loop(), threading.get_ident())
        assert (code, redirect_uri) == ("c", "http://localhost:8080/cb")
        return credentials

    def fake_userinfo(creds: object) -> dict[str, str]:
        seen["userinfo"] = (_ran_on_loop(), threading.get_ident())
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
    loop_thread = threading.get_ident()
    assert seen["exchange"][0] is False
    assert seen["userinfo"][0] is False
    assert seen["exchange"][1] != loop_thread
    assert seen["userinfo"][1] != loop_thread


async def test_google_userinfo_failure_off_loop_still_redirects_and_logs() -> None:
    """``to_thread`` re-raises: the failure lane and its log line are unchanged."""
    session_mgr = MagicMock()
    session_mgr._redis.get.return_value = "pending"
    oauth_mgr = MagicMock()
    oauth_mgr.exchange_code_web.return_value = object()
    oauth_mgr.get_user_info_web.side_effect = RuntimeError("userinfo-internal-detail")
    fake_logger = MagicMock()
    with (
        patch.object(auth_module, "_session_manager", session_mgr),
        patch.object(auth_module, "_oauth2_manager", oauth_mgr),
        patch.object(auth_module, "logger", fake_logger),
        patch.dict(os.environ, {"GOOGLE_REDIRECT_URI": "http://localhost:8080/cb"}),
    ):
        resp = await google_callback(None, code="c", state="s", error=None, error_description=None)

    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert "error=oauth_failed" in loc
    assert "userinfo-internal-detail" not in loc
    fake_logger.error.assert_called_once_with("OAuth2 callback failed: userinfo-internal-detail")
