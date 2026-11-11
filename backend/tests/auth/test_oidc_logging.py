"""Synthetic OIDC credentials never escape Authlib's DEBUG token records."""

from __future__ import annotations

import io
import logging

import pytest

from auth.oidc_logging import install_oidc_log_redaction, redact_oidc_credentials
from utils.logger import _RedactingStdlibFormatter, _scrub_text, redact_pg_detail

_ACCESS = "kmc_oidc_at_" + "a" * 43
_REFRESH = "kmc_oidc_rt_" + "b" * 43
_CODE = "kmc_oidc_code_" + "c" * 43


@pytest.mark.parametrize(
    "name",
    [
        "authlib.oauth2.rfc6749.grants.authorization_code",
        "authlib.oauth2.rfc6749.grants.refresh_token",
    ],
)
def test_authlib_debug_records_are_scrubbed_without_disabling_logging(name, caplog):
    install_oidc_log_redaction()
    logger = logging.getLogger(name)
    token = {"access_token": _ACCESS, "refresh_token": _REFRESH, "scope": "openid profile"}
    with caplog.at_level(logging.DEBUG, logger=name):
        logger.debug("Issue token %r to %r", token, "external-web")
        logger.debug("Validate token request of %r", "external-web")
    assert _ACCESS not in caplog.text
    assert _REFRESH not in caplog.text
    assert "Validate token request" in caplog.text
    assert "openid profile" in caplog.text
    assert "external-web" in caplog.text
    assert "[redacted]" in caplog.text


def test_delayed_handlers_cannot_observe_later_id_token_mutation(caplog):
    install_oidc_log_redaction()
    name = "authlib.oauth2.rfc6749.grants.authorization_code"
    token = {"access_token": _ACCESS, "refresh_token": _REFRESH}
    with caplog.at_level(logging.DEBUG, logger=name):
        logging.getLogger(name).debug("Issue token %r to %r", token, "external-web")
    token["id_token"] = "later-signed-id-token-must-not-be-captured"
    record = next(record for record in caplog.records if "Issue token" in record.getMessage())
    assert record.args == ()
    assert "later-signed-id-token" not in record.getMessage()
    assert _ACCESS not in record.getMessage()


def test_installer_is_idempotent_and_preserves_other_oauth_messages(caplog):
    install_oidc_log_redaction()
    name = "authlib.oauth2.rfc6749.grants.refresh_token"
    logger = logging.getLogger(name)
    first_count = len(logger.filters)
    install_oidc_log_redaction()
    assert len(logger.filters) == first_count
    with caplog.at_level(logging.DEBUG, logger=name):
        logger.debug("Issue token %r", {"access_token": "unrelated-mcp-test-token"})
    assert "unrelated-mcp-test-token" in caplog.text


def test_prefix_redaction_covers_codes_and_preserves_diagnostic_structure():
    rendered = redact_oidc_credentials(f"code={_CODE} access={_ACCESS} refresh={_REFRESH} count=3")
    for credential in (_CODE, _ACCESS, _REFRESH):
        assert credential not in rendered
    assert "count=3" in rendered
    assert redact_oidc_credentials(rendered) == rendered
    assert redact_oidc_credentials("ordinary diagnostic") == "ordinary diagnostic"


def test_shared_structured_and_stdlib_chokepoints_scrub_oidc_prefixes():
    event = redact_pg_detail(None, "error", {"event": "failed", "nested": [{"code": _CODE}]})
    assert _CODE not in repr(event)
    assert _ACCESS not in _scrub_text(f"GET /oidc/userinfo?access_token={_ACCESS}")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_RedactingStdlibFormatter())
    record = logging.LogRecord(
        "oidc-test", logging.ERROR, __file__, 1, "failed %s", (_REFRESH,), None
    )
    handler.handle(record)
    assert _REFRESH not in stream.getvalue()
