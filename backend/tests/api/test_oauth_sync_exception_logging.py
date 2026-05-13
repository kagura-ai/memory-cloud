"""Regression guard for Issue #637 / PR #636 — exception logging in ``_run_oauth_sync``.

WHY: existing oauth tests only mock-patch ``_run_oauth_sync`` itself, so the real
``except Exception:`` block is never exercised. PR #636's ``logger.exception(...)``
line could be silently deleted in a future refactor with no test failing.

This pins three contracts at once for both action values
(``create_token_response`` and ``create_authorization_response``):

* the exception still propagates (re-raise intact),
* exactly one ``oauth_sync_failed`` structlog event is emitted with the
  expected ``action`` kwarg, and
* the synchronous DB session is rolled back AND closed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from api.routes.oauth import _run_oauth_sync


@pytest.mark.parametrize(
    "action",
    ["create_token_response", "create_authorization_response"],
)
def test_run_oauth_sync_logs_exception_before_reraise(action: str) -> None:
    fake_server = MagicMock()
    getattr(fake_server, action).side_effect = RuntimeError("simulated authlib failure")
    fake_session = MagicMock()

    with (
        patch("api.routes.oauth.create_authorization_server", return_value=fake_server),
        patch("api.routes.oauth.get_sync_session", return_value=fake_session),
        capture_logs() as captured,
    ):
        with pytest.raises(RuntimeError, match="simulated authlib failure"):
            _run_oauth_sync(action, request=MagicMock())

    log_events = [r for r in captured if r.get("event") == "oauth_sync_failed"]
    assert len(log_events) == 1
    assert log_events[0]["action"] == action
    fake_session.rollback.assert_called_once()
    fake_session.close.assert_called_once()
