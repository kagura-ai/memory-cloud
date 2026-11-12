"""Targeted redaction for the isolated provider's wire credential prefixes."""

from __future__ import annotations

import logging
import re
from typing import Any

_CREDENTIAL = re.compile(r"(kmc_oidc_(?:code|at|rt)_)[A-Za-z0-9_-]+")
_AUTHLIB_GRANT_LOGGERS = (
    "authlib.oauth2.rfc6749.grants.authorization_code",
    "authlib.oauth2.rfc6749.grants.refresh_token",
)


def redact_oidc_credentials(text: str) -> str:
    """Scrub only this provider's codes/access/refresh tokens from rendered text."""
    return _CREDENTIAL.sub(r"\1[redacted]", text)


def _contains_credential(value: Any, depth: int = 0) -> bool:
    if isinstance(value, str):
        return _CREDENTIAL.search(value) is not None
    if depth >= 8:
        return False
    if isinstance(value, dict):
        return any(
            _contains_credential(key, depth + 1) or _contains_credential(item, depth + 1)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_credential(item, depth + 1) for item in value)
    return False


class _OIDCCredentialFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if _contains_credential(record.msg) or _contains_credential(record.args):
            # Authlib passes its mutable token dictionary as a formatting arg,
            # before the OpenID extension appends the ID token. Freeze a clean
            # string now: a queue/capture handler cannot format that later
            # mutation, and handlers never receive the original credentials.
            try:
                record.msg = redact_oidc_credentials(record.getMessage())
            except Exception:
                record.msg = "OIDC credential-bearing diagnostic could not be formatted safely"
            record.args = ()
        return True


def install_oidc_log_redaction() -> None:
    """Install idempotent source filters without disabling OAuth diagnostics.

    Call before constructing an isolated authorization server. Other OAuth
    credentials and logger levels/handlers are untouched.
    """
    for name in _AUTHLIB_GRANT_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(item, _OIDCCredentialFilter) for item in logger.filters):
            logger.addFilter(_OIDCCredentialFilter())
