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


def _find_grant_call(wrapper, grant_cls):
    """Locate the ``register_grant`` call for a given grant class.

    Iterate ``call_args_list`` and return the matching call rather than
    indexing ``[0]``. Robust against grant ordering changes inside
    ``_register_grants``.
    """
    for call in wrapper.server.register_grant.call_args_list:
        if call.args and call.args[0] is grant_cls:
            return call
    raise AssertionError(
        f"register_grant was never called with {grant_cls.__name__}. "
        f"Recorded calls: {wrapper.server.register_grant.call_args_list!r}"
    )


class TestPkceExtensionRegistration:
    """Pin that PKCE is enforced via Authlib's CodeChallenge extension."""

    def test_authorization_code_grant_registered_with_code_challenge_extension(self):
        wrapper = _build_wrapper_with_mocked_server()
        wrapper._register_grants()

        call = _find_grant_call(wrapper, AuthorizationCodeGrant)
        assert len(call.args) >= 2, (
            "AuthorizationCodeGrant must be registered with an extensions list "
            "(positional arg 2). Authlib's CodeChallenge extension is required "
            "to enforce PKCE for public clients (issue #513)."
        )
        extensions = call.args[1]
        assert any(isinstance(e, CodeChallenge) for e in extensions), (
            f"CodeChallenge extension missing from grant extensions {extensions!r}"
        )

    def test_pkce_required_defaults_to_true(self):
        wrapper = _build_wrapper_with_mocked_server()
        wrapper._register_grants()

        call = _find_grant_call(wrapper, AuthorizationCodeGrant)
        extensions = call.args[1]
        code_challenge = next(e for e in extensions if isinstance(e, CodeChallenge))
        assert code_challenge.required is True, (
            "PKCE must be required by default. The default in settings "
            "(oauth_pkce_required) is True; flipping it to False should require "
            "an explicit env var, not be the default."
        )

    def test_pkce_required_true_registers_extension(self):
        """``oauth_pkce_required=True`` registers the extension with required=True."""
        with patch("auth.oauth2_server.get_settings") as mock_get_settings:
            mock_settings = MagicMock()
            mock_settings.oauth_pkce_required = True
            mock_get_settings.return_value = mock_settings

            wrapper = _build_wrapper_with_mocked_server()
            wrapper._register_grants()

        call = _find_grant_call(wrapper, AuthorizationCodeGrant)
        assert len(call.args) >= 2, "extensions list must be registered when required"
        extensions = call.args[1]
        code_challenge = next(e for e in extensions if isinstance(e, CodeChallenge))
        assert code_challenge.required is True

    def test_pkce_required_false_skips_extension_for_true_rollback(self):
        """The kill-switch off path must SKIP CodeChallenge entirely.

        Authlib's ``CodeChallenge`` extension still enforces ``code_verifier``
        whenever a ``code_challenge`` is stored on the authorization code,
        even when ``required=False`` (the "challenge stored → verifier
        required" branch fires regardless of the flag). To make
        ``OAUTH_PKCE_REQUIRED=false`` a true pre-#513-equivalent rollback,
        we must not register the extension at all when the flag is off.
        """
        with patch("auth.oauth2_server.get_settings") as mock_get_settings:
            mock_settings = MagicMock()
            mock_settings.oauth_pkce_required = False
            mock_get_settings.return_value = mock_settings

            wrapper = _build_wrapper_with_mocked_server()
            wrapper._register_grants()

        call = _find_grant_call(wrapper, AuthorizationCodeGrant)
        # Either no extensions arg at all, or a list without CodeChallenge.
        if len(call.args) >= 2:
            extensions = call.args[1] or []
            assert not any(isinstance(e, CodeChallenge) for e in extensions), (
                "CodeChallenge must NOT be registered when oauth_pkce_required=False — "
                "registering with required=False still enforces the verifier when a "
                "challenge is stored, so it's not a true rollback to pre-#513 behavior."
            )


class TestPkceCodeVerifierEnforcement:
    """Pin the runtime behavior of the registered ``CodeChallenge`` extension.

    The ``TestPkceExtensionRegistration`` tests above prove the extension is
    correctly wired into ``AuthorizationCodeGrant``. These tests prove the
    wired-in extension actually rejects token exchange when the spec demands
    it. Together they cover the wire-level PKCE enforcement contract: any
    ``token_endpoint_auth_method="none"`` client that reaches ``/token``
    without a ``code_verifier`` is rejected by Authlib's
    ``after_validate_token_request`` hook with ``InvalidRequestError`` —
    which FastAPI translates to HTTP 400.

    A full HTTP TestClient round-trip would also exercise the wire format,
    but it would require a sync DB session, a saved ``OAuth2AuthorizationCode``
    row, and an OAuth2Client row, all per-test. These tests exercise the same
    Authlib hook directly so the regression coverage holds without that setup
    cost.

    Authlib 1.3.x had ``validate_code_verifier(self, grant)``; 1.4+ added a
    ``result`` parameter (``self, grant, result``). The pyproject.toml floor
    is ``authlib>=1.3.0`` and CI installs latest, so the test must work
    across both signatures — ``_invoke_validate_code_verifier`` introspects
    the live signature and passes a ``MagicMock`` for ``result`` when needed.
    """

    @staticmethod
    def _build_grant(*, auth_method: str, code_verifier: str | None = None):
        """Construct a minimal mock grant in the shape Authlib's hook expects."""
        grant = MagicMock()
        grant.request = MagicMock()
        grant.request.form = {} if code_verifier is None else {"code_verifier": code_verifier}
        grant.request.auth_method = auth_method
        grant.request.authorization_code = MagicMock()
        return grant

    @staticmethod
    def _invoke_validate_code_verifier(cc: CodeChallenge, grant: MagicMock) -> None:
        """Call ``validate_code_verifier`` portably across Authlib 1.3 / 1.4+.

        1.3.x: ``(self, grant)``  → 0 extra positional args.
        1.4+:  ``(self, grant, result)`` → 1 extra positional arg.
        """
        import inspect

        params = inspect.signature(cc.validate_code_verifier).parameters
        # `params` excludes `self` (bound method); count remaining positionals.
        extra_args = max(0, len(params) - 1)
        cc.validate_code_verifier(grant, *([MagicMock()] * extra_args))

    def test_none_auth_without_verifier_raises_invalid_request(self):
        from authlib.oauth2.rfc6749.errors import InvalidRequestError

        cc = CodeChallenge(required=True)
        grant = self._build_grant(auth_method="none", code_verifier=None)

        with pytest.raises(InvalidRequestError) as exc_info:
            self._invoke_validate_code_verifier(cc, grant)
        assert "code_verifier" in str(exc_info.value).lower(), (
            f"expected error message to mention 'code_verifier', got: {exc_info.value}"
        )

    def test_confidential_client_without_verifier_is_allowed(self):
        """Confidential clients (auth_method != 'none') skip the PKCE gate."""
        cc = CodeChallenge(required=True)
        grant = self._build_grant(auth_method="client_secret_basic", code_verifier=None)
        # Patch get_authorization_code_challenge so the second branch in
        # validate_code_verifier (challenge-stored-but-no-verifier) doesn't trip.
        cc.get_authorization_code_challenge = lambda code: None  # type: ignore[method-assign]

        # Must NOT raise: the `required` flag is gated on auth_method == 'none'.
        # A confidential client without verifier is allowed — it authenticates
        # via client_secret instead.
        self._invoke_validate_code_verifier(cc, grant)

    def test_required_false_does_not_force_verifier_for_none_clients(self):
        """When the kill-switch flips ``required`` to False, the gate disengages.

        This pins the rollback contract: setting ``OAUTH_PKCE_REQUIRED=false``
        must allow ``none``-auth clients without ``code_verifier`` through
        (matching the pre-#513 behavior).

        Note: in ``_register_grants`` we now SKIP the extension entirely when
        the kill-switch is off (because ``CodeChallenge(required=False)`` still
        enforces the verifier when a challenge is stored). This test verifies
        the narrower contract that ``required=False`` does not gate the
        no-challenge-no-verifier baseline path — useful as a sanity check on
        Authlib's flag semantics.
        """
        cc = CodeChallenge(required=False)
        grant = self._build_grant(auth_method="none", code_verifier=None)
        cc.get_authorization_code_challenge = lambda code: None  # type: ignore[method-assign]

        # Must NOT raise.
        self._invoke_validate_code_verifier(cc, grant)
