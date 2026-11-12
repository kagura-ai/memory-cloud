"""Which provider sign-ins may link to an email sign-up account (#1734).

Owner decision: Google only when it is authoritative for the mailbox
(verified, and a Workspace account or a Gmail address); GitHub never in v1.
The decision lives in one function, and both callbacks pass it to
``ensure_user`` as ``email_authoritative``.
"""

from __future__ import annotations

import inspect

import pytest

from api.routes import auth as auth_routes
from api.routes.auth import provider_authoritative_for_address


@pytest.mark.parametrize(
    ("user_info", "expected"),
    [
        ({"email": "a@gmail.com", "email_verified": True}, True),
        ({"email": "a@GoogleMail.com", "email_verified": True}, True),
        ({"email": "a@corp.example", "email_verified": True, "hd": "corp.example"}, True),
        ({"email": "a@corp.example", "email_verified": True}, False),
        ({"email": "a@corp.example", "email_verified": True, "hd": "  "}, False),
        ({"email": "a@gmail.com", "email_verified": False}, False),
        ({"email": "a@gmail.com", "email_verified": "true"}, False),
        ({"email": "a@gmail.com"}, False),
        ({"email": "a@corp.example", "email_verified": False, "hd": "corp.example"}, False),
        ({"email": "not-an-address", "email_verified": True}, False),
    ],
)
def test_google(user_info: dict, expected: bool) -> None:
    assert provider_authoritative_for_address("google", user_info) is expected


@pytest.mark.parametrize(
    "user_info",
    [
        {"email": "a@gmail.com", "email_verified": True},
        {"email": "a@corp.example", "email_verified": True, "hd": "corp.example"},
    ],
)
def test_github_never_links_automatically(user_info: dict) -> None:
    assert provider_authoritative_for_address("github", user_info) is False


@pytest.mark.parametrize(
    ("callback", "provider"),
    [(auth_routes.google_callback, "google"), (auth_routes.github_callback, "github")],
)
def test_both_callbacks_pass_the_decision_to_ensure_user(callback, provider: str) -> None:
    source = inspect.getsource(callback)
    assert (
        f'email_authoritative=provider_authoritative_for_address("{provider}", user_info)' in source
    )
