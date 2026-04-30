"""Regression test for OAuth2 PKCE enforcement (issue #513).

Issue #157 added ``token_endpoint_auth_method="none"`` (public client) support
and PKCE plumbing through the authorization flow but never registered Authlib's
``CodeChallenge`` extension. As a result, ``code_verifier`` was never validated
at the token endpoint — a public client could exchange an authorization code
for a token without proving possession of the original ``code_challenge``.

Issue #513 closes the gap by registering ``CodeChallenge(required=True)`` on
``AuthorizationCodeGrant``. This test pins the wiring: it asserts that
``_register_grants`` invokes ``server.register_grant`` with the
``CodeChallenge`` extension, and that the ``required`` flag follows
``settings.oauth_pkce_required`` so the kill-switch works.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Match the sys.path layout the rest of the backend tests use.
_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

import pytest  # noqa: E402
from authlib.oauth2.rfc7636 import CodeChallenge  # noqa: E402

from auth.oauth2_server import (  # noqa: E402
    AuthorizationCodeGrant,
    OAuth2AuthorizationServer,
)


def _build_wrapper_with_mocked_server():
    """Construct an ``OAuth2AuthorizationServer`` whose underlying Authlib
    server is replaced with a mock, so we can introspect ``register_grant``
    calls without spinning up a real DB session."""
    wrapper = OAuth2AuthorizationServer.__new__(OAuth2AuthorizationServer)
    wrapper.session = MagicMock()
    wrapper.server = MagicMock()
    return wrapper


class TestPkceExtensionRegistration:
    """Pin that PKCE is enforced via Authlib's CodeChallenge extension."""

    def test_authorization_code_grant_registered_with_code_challenge_extension(self):
        wrapper = _build_wrapper_with_mocked_server()
        wrapper._register_grants()

        # First register_grant call is for AuthorizationCodeGrant.
        first_call = wrapper.server.register_grant.call_args_list[0]
        grant_cls, *rest = first_call.args
        assert grant_cls is AuthorizationCodeGrant, (
            f"expected first register_grant call to be AuthorizationCodeGrant, got {grant_cls!r}"
        )
        assert rest, (
            "AuthorizationCodeGrant must be registered with an extensions list "
            "(positional arg 2). Authlib's CodeChallenge extension is required "
            "to enforce PKCE for public clients (issue #513)."
        )
        extensions = rest[0]
        assert any(isinstance(e, CodeChallenge) for e in extensions), (
            f"CodeChallenge extension missing from grant extensions {extensions!r}"
        )

    def test_pkce_required_defaults_to_true(self):
        wrapper = _build_wrapper_with_mocked_server()
        wrapper._register_grants()

        extensions = wrapper.server.register_grant.call_args_list[0].args[1]
        code_challenge = next(e for e in extensions if isinstance(e, CodeChallenge))
        assert code_challenge.required is True, (
            "PKCE must be required by default. The default in settings "
            "(oauth_pkce_required) is True; flipping it to False should require "
            "an explicit env var, not be the default."
        )

    @pytest.mark.parametrize("required_setting", [True, False])
    def test_pkce_required_follows_settings(self, required_setting):
        """The kill-switch must propagate from settings.oauth_pkce_required."""
        with patch("auth.oauth2_server.get_settings") as mock_get_settings:
            mock_settings = MagicMock()
            mock_settings.oauth_pkce_required = required_setting
            mock_get_settings.return_value = mock_settings

            wrapper = _build_wrapper_with_mocked_server()
            wrapper._register_grants()

        extensions = wrapper.server.register_grant.call_args_list[0].args[1]
        code_challenge = next(e for e in extensions if isinstance(e, CodeChallenge))
        assert code_challenge.required is required_setting
