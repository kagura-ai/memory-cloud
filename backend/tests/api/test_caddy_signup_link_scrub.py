"""The shipped proxy config scrubs emailed one-time links from its logs (#1734).

``/signup/verify?token=`` creates an account and ``/password/reset?token=`` /
``/password/setup?token=`` set a password; all three travel in a query string.
Every copy of the scrub (access log, Location, Refresh, default logger) must
redact the token and keep the rest of the line.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_TEMPLATE = Path(__file__).resolve().parents[3] / "terraform/single-server/Caddyfile.tpl"
_RULE = re.compile(
    r"(?P<field>request>uri|resp_headers>Location|resp_headers>Refresh) regexp "
    r"`(?P<pattern>[^`]+)` `(?P<replacement>[^`]+)`"
)


def _apply(rule: re.Match[str], text: str) -> str:
    def substitute(match: re.Match[str]) -> str:
        return re.sub(
            r"\$\{(\d+)\}", lambda group: match.group(int(group[1])) or "", rule["replacement"]
        )

    return re.sub(rule["pattern"], substitute, text)


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        ("/signup/verify?token=abcDEF_123-x", "/signup/verify?token=REDACTED"),
        ("/password/reset?token=abc123", "/password/reset?token=REDACTED"),
        ("/password/setup?lang=ja&token=abc123&x=1", "/password/setup?lang=ja&token=REDACTED&x=1"),
        ("/api/v1/memories?limit=5&q=token", "/api/v1/memories?limit=5&q=token"),
        ("/callback?refresh_token=keep", "/callback?refresh_token=keep"),
    ],
)
def test_every_rule_redacts_a_token_query_parameter(original: str, expected: str) -> None:
    rules = list(_RULE.finditer(_TEMPLATE.read_text(encoding="utf-8")))
    assert len(rules) == 4
    for rule in rules:
        assert _apply(rule, original) == expected
