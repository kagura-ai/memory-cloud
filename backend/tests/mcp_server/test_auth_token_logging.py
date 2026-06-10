"""Regression test for Issue #965 — Bearer token over-exposure in DEBUG/WARNING logs.

``authenticate_mcp_request`` used to log ``token[:20]`` at both the DEBUG
"auth attempt" line and the WARNING "auth failed" line. API keys are
``kagura_<43-char base64>``, so 20 chars leaked ~13 characters of entropy
beyond the known ``kagura_`` prefix — a partial-key oracle in a shared log
pipeline. The fix logs at most the 8-char ``kagura_`` prefix.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from mcp_server.auth import authenticate_mcp_request  # noqa: E402
from utils.exceptions import AuthenticationError  # noqa: E402

# kagura_ prefix (8 chars incl. underscore) + secret body. The body must never
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
    # Not even a leading slice longer than the 8-char prefix should leak.
    assert _SECRET_BODY[:13] not in logged
