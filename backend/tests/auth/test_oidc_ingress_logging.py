"""The ingress scrubs OIDC codes in redirect headers as well as request URLs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_TEMPLATE = Path(__file__).resolve().parents[3] / "terraform/single-server/Caddyfile.tpl"
_RULE = re.compile(
    r"(?P<field>request>uri|resp_headers>Location|resp_headers>Refresh) regexp "
    r"`(?P<pattern>[^`]+)` `(?P<replacement>[^`]+)`"
)


def _apply(rule, text):
    def substitute(match):
        return re.sub(
            r"\$\{(\d+)\}",
            lambda group: match.group(int(group[1])) or "",
            rule["replacement"],
        )

    return re.sub(rule["pattern"], substitute, text)


@pytest.mark.parametrize("kind", ["code", "at", "rt"])
def test_default_and_access_log_rules_scrub_oidc_credentials(kind):
    rules = list(_RULE.finditer(_TEMPLATE.read_text(encoding="utf-8")))
    assert [rule["field"] for rule in rules].count("request>uri") == 2
    assert {rule["field"] for rule in rules} == {
        "request>uri",
        "resp_headers>Location",
        "resp_headers>Refresh",
    }
    secret = "kmc_oidc_" + kind + "_" + "x" * 43
    for rule in rules:
        cleaned = _apply(rule, f"https://app.example.test/callback?code={secret}&state=state")
        assert secret not in cleaned
        assert "REDACTED" in cleaned
        assert "state=state" in cleaned


@pytest.mark.parametrize(
    "original,expected",
    [
        ("/join/private-invite", "/join/REDACTED"),
        ("/api/v1/beta-invites/private-invite/preview", "/api/v1/beta-invites/REDACTED/preview"),
        ("/login?invite=private-invite", "/login?invite=REDACTED"),
        ("/invite/private-invite", "/invite/REDACTED"),
        ("/api/v1/invitations/" + "x" * 25, "/api/v1/invitations/REDACTED"),
        ("/login?return_to=%2Finvite%2Fprivate-invite", "/login?return_to=%2Finvite%2FREDACTED"),
    ],
)
def test_extending_oidc_redaction_preserves_every_existing_invite_shape(original, expected):
    for rule in _RULE.finditer(_TEMPLATE.read_text(encoding="utf-8")):
        assert _apply(rule, original) == expected
