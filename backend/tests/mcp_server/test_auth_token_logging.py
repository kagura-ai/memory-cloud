"""Logging tests for ``authenticate_mcp_request``.

Issue #965 — Bearer token over-exposure in DEBUG/WARNING logs.

``authenticate_mcp_request`` used to log ``token[:20]`` at both the DEBUG
"auth attempt" line and the WARNING "auth failed" line. API keys are
``kagura_<43-char base64>``, so 20 chars leaked ~13 characters of entropy
beyond the known ``kagura_`` prefix — a partial-key oracle in a shared log
pipeline. The fix logs at most an 8-char prefix (``kagura_`` is 7 chars, so
this is the prefix plus a single body char — matching the repo-wide
``token[:8]`` redaction convention).

Issue #2025 — a successful auth is per-request noise: it logs at DEBUG, never
INFO, while a failed auth keeps its WARNING line.
"""

from __future__ import annotations

import logging
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from mcp_server.auth import OAuthGrant, authenticate_mcp_request  # noqa: E402
from utils.exceptions import AuthenticationError  # noqa: E402

# kagura_ prefix (7 chars incl. underscore) + secret body. The body must never
# appear in any log record.
_SECRET_BODY = "AbCdEf0123456789SECRETxyzSECRETxyzSECRETxyz"
_TOKEN = f"kagura_{_SECRET_BODY}"


@pytest.mark.asyncio
async def test_failed_auth_does_not_log_token_secret(caplog):
    """A failed auth WARNING/DEBUG line must not expose token chars past prefix."""
    with (
        patch("mcp_server.auth._verify_api_key", new=AsyncMock(return_value=None)),
        patch("mcp_server.auth._verify_oauth2_token", new=AsyncMock(return_value=None)),
        caplog.at_level(logging.DEBUG, logger="mcp_server.auth"),
    ):
        with pytest.raises(AuthenticationError):
            await authenticate_mcp_request(f"Bearer {_TOKEN}")

    logged = "\n".join(record.getMessage() for record in caplog.records)
    # The secret body must never appear anywhere in the logs.
    assert _SECRET_BODY not in logged
    # The old token[:20] regression leaked exactly _SECRET_BODY[:13]; guard it.
    assert _SECRET_BODY[:13] not in logged
    # Upper bound: at most 8 token chars may be logged. _TOKEN[:9] only appears
    # if the slice widened past [:8] — this catches any token[:9..20] regression
    # (e.g. token[:19]), not just the original token[:20].
    assert _TOKEN[:9] not in logged


_SESSION_COOKIE = ("kagura" + "_session" + "=sid000").encode()


def _records_at_or_above_info(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "mcp_server.auth" and r.levelno >= logging.INFO]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "header", "cookie", "patches"),
    [
        (
            "session_cookie",
            None,
            _SESSION_COOKIE,
            {"mcp_server.auth._verify_session_cookie": "cookie-user"},
        ),
        (
            "api_key",
            f"Bearer {_TOKEN}",
            None,
            {"mcp_server.auth._verify_api_key": ("u-1", None, None)},
        ),
        (
            "oauth2",
            "Bearer opaque_oauth_token",
            None,
            {
                "mcp_server.auth._verify_api_key": None,
                "mcp_server.auth._verify_oauth2_token": OAuthGrant(
                    "oauth-user", "memory:read", None
                ),
            },
        ),
    ],
)
async def test_successful_auth_logs_at_debug_not_info(caplog, method, header, cookie, patches):
    """#2025: one INFO line per authenticated request is noise — success is DEBUG."""
    with ExitStack() as stack:
        for target, value in patches.items():
            stack.enter_context(patch(target, new=AsyncMock(return_value=value)))
        stack.enter_context(caplog.at_level(logging.DEBUG, logger="mcp_server.auth"))
        await authenticate_mcp_request(header, cookie_header=cookie)

    assert _records_at_or_above_info(caplog) == []
    success = [r for r in caplog.records if f"MCP auth success: method={method}" in r.getMessage()]
    assert [r.levelno for r in success] == [logging.DEBUG]


@pytest.mark.asyncio
async def test_failed_auth_still_logs_warning(caplog):
    """#2025: demoting success must not silence failures."""
    with (
        patch("mcp_server.auth._verify_api_key", new=AsyncMock(return_value=None)),
        patch("mcp_server.auth._verify_oauth2_token", new=AsyncMock(return_value=None)),
        caplog.at_level(logging.DEBUG, logger="mcp_server.auth"),
    ):
        with pytest.raises(AuthenticationError):
            await authenticate_mcp_request(f"Bearer {_TOKEN}")

    failed = [r for r in caplog.records if "MCP auth failed" in r.getMessage()]
    assert [r.levelno for r in failed] == [logging.WARNING]
