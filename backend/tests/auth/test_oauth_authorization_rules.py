"""Unit tests for the authorization request rules in ``auth.oauth2_server`` (#1686).

Scope (RFC 6749 §3.3), PKCE with ``S256`` only (RFC 7636) and resource
indicators (RFC 8707). The end-to-end behaviour through the endpoints is in
``tests/api/test_oauth_authorization_code_flow.py``.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

import pytest  # noqa: E402
from authlib.oauth2.rfc6749.errors import (  # noqa: E402
    InvalidGrantError,
    InvalidRequestError,
    InvalidScopeError,
)
from authlib.oauth2.rfc6749.requests import BasicOAuth2Payload  # noqa: E402
from authlib.oauth2.rfc7636 import create_s256_code_challenge  # noqa: E402

from api.routes.well_known import oauth_protected_resource  # noqa: E402
from auth import oauth2_server as mod  # noqa: E402
from auth.mcp_scopes import DCR_DEFAULT_SCOPE  # noqa: E402
from auth.oauth2_server import (  # noqa: E402
    AuthorizationCodeGrant,
    InvalidTargetError,
    OAuth2AuthorizationServer,
    S256CodeChallenge,
    check_code_challenge,
    granted_scope,
    mcp_resource_identifier,
    requested_resource,
    same_resource,
    validate_authorization_parameters,
)

MCP = "https://memory.example.test/mcp"


@pytest.fixture(autouse=True)
def _frontend_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRONTEND_URL", "https://memory.example.test")
    monkeypatch.delenv("MCP_BASE_PATH", raising=False)


def _payload(**params: str) -> BasicOAuth2Payload:
    return BasicOAuth2Payload(params)


def _multi_payload(**params: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        data={key: values[-1] for key, values in params.items()},
        datalist=params,
    )


def _client(auth_method: str = "none", scope: str = DCR_DEFAULT_SCOPE) -> SimpleNamespace:
    return SimpleNamespace(token_endpoint_auth_method=auth_method, scope=scope)


# ---------------------------------------------------------------------------
# granted_scope
# ---------------------------------------------------------------------------


class TestGrantedScope:
    @pytest.mark.parametrize(
        ("requested", "registered", "granted"),
        [
            ("memory:read memory:write", DCR_DEFAULT_SCOPE, "memory:read memory:write"),
            # An undefined scope is dropped.
            ("memory:read undefined:scope", DCR_DEFAULT_SCOPE, "memory:read"),
            # memory:admin needs the client's registration.
            ("memory:read memory:admin", DCR_DEFAULT_SCOPE, "memory:read"),
            ("memory:admin", "memory:read memory:admin", "memory:admin"),
            # No scope requested: the registered scope.
            (None, "memory:read memory:write", "memory:read memory:write"),
            ("", "memory:read memory:write", "memory:read memory:write"),
            ("   ", "memory:read", "memory:read"),
            # A registered scope the server does not define is not granted.
            (None, "memory:read legacy:scope", "memory:read"),
            # Request order, duplicates dropped.
            (
                "memory:write memory:read memory:write",
                DCR_DEFAULT_SCOPE,
                "memory:write memory:read",
            ),
            # Nothing grantable.
            ("undefined:scope", DCR_DEFAULT_SCOPE, ""),
            ("memory:read", "", ""),
        ],
    )
    def test_granted_scope(self, requested: str | None, registered: str, granted: str) -> None:
        assert granted_scope(requested, registered) == granted


# ---------------------------------------------------------------------------
# resource
# ---------------------------------------------------------------------------


class TestResource:
    async def test_identifier_matches_the_protected_resource_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for frontend, mcp_path in (
            ("https://memory.example.test", None),
            ("https://memory.example.test/", None),
            ("http://127.0.0.1:8080", "/custom-mcp"),
        ):
            monkeypatch.setenv("FRONTEND_URL", frontend)
            if mcp_path is None:
                monkeypatch.delenv("MCP_BASE_PATH", raising=False)
            else:
                monkeypatch.setenv("MCP_BASE_PATH", mcp_path)
            published = (await oauth_protected_resource())["resource"]
            assert mcp_resource_identifier() == published

    @pytest.mark.parametrize(
        ("first", "second", "same"),
        [
            (MCP, MCP, True),
            (MCP, f"{MCP}/", True),
            (MCP, "HTTPS://Memory.Example.Test/mcp", True),
            (MCP, "https://memory.example.test/mcp//", False),
            (MCP, "https://memory.example.test/mcp/sse", False),
            (MCP, "https://other.example/mcp", False),
            (MCP, "https://memory.example.test/mcp#fragment", False),
            (MCP, "http://memory.example.test/mcp", False),
            (MCP, "http://[::1", False),
        ],
    )
    def test_same_resource(self, first: str, second: str, same: bool) -> None:
        assert same_resource(first, second) is same

    def test_absent_or_empty_resource(self) -> None:
        assert requested_resource(_payload()) is None
        assert requested_resource(_payload(resource="")) is None

    @pytest.mark.parametrize("value", [MCP, f"{MCP}/"])
    def test_mcp_resource_is_returned_as_published(self, value: str) -> None:
        assert requested_resource(_payload(resource=value)) == MCP

    @pytest.mark.parametrize(
        "value", ["https://other.example/mcp", "https://memory.example.test/api", "not a url"]
    )
    def test_other_resource_is_invalid_target(self, value: str) -> None:
        with pytest.raises(InvalidTargetError) as excinfo:
            requested_resource(_payload(resource=value))
        assert excinfo.value.error == "invalid_target"

    def test_every_value_must_name_the_mcp_resource(self) -> None:
        assert requested_resource(_multi_payload(resource=[MCP, f"{MCP}/"])) == MCP
        with pytest.raises(InvalidTargetError):
            requested_resource(_multi_payload(resource=[MCP, "https://other.example/mcp"]))


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


_CHALLENGE = create_s256_code_challenge(secrets.token_urlsafe(48))


class TestCheckCodeChallenge:
    def test_s256_challenge_is_accepted(self) -> None:
        payload = _payload(code_challenge=_CHALLENGE, code_challenge_method="S256")
        check_code_challenge(payload, _client(), required=True)

    @pytest.mark.parametrize("method", ["plain", "s256", "S512"])
    def test_other_methods_are_refused(self, method: str) -> None:
        payload = _payload(code_challenge=_CHALLENGE, code_challenge_method=method)
        with pytest.raises(InvalidRequestError, match="S256"):
            check_code_challenge(payload, _client(), required=True)

    def test_omitted_method_is_refused(self) -> None:
        with pytest.raises(InvalidRequestError, match="S256"):
            check_code_challenge(_payload(code_challenge=_CHALLENGE), _client(), required=True)

    def test_method_without_challenge_is_refused(self) -> None:
        with pytest.raises(InvalidRequestError, match="code_challenge"):
            check_code_challenge(_payload(code_challenge_method="S256"), _client(), required=True)

    def test_malformed_challenge_is_refused(self) -> None:
        payload = _payload(code_challenge="short", code_challenge_method="S256")
        with pytest.raises(InvalidRequestError, match="Invalid 'code_challenge'"):
            check_code_challenge(payload, _client(), required=True)

    def test_repeated_challenge_is_refused(self) -> None:
        payload = _multi_payload(
            code_challenge=[_CHALLENGE, _CHALLENGE], code_challenge_method=["S256"]
        )
        with pytest.raises(InvalidRequestError, match="Multiple"):
            check_code_challenge(payload, _client(), required=True)

    def test_public_client_must_send_a_challenge(self) -> None:
        with pytest.raises(InvalidRequestError, match="code_challenge"):
            check_code_challenge(_payload(), _client("none"), required=True)

    def test_confidential_client_may_omit_pkce(self) -> None:
        check_code_challenge(_payload(), _client("client_secret_post"), required=True)

    def test_not_required_lets_a_public_client_omit_pkce(self) -> None:
        check_code_challenge(_payload(), _client("none"), required=False)


class TestS256CodeVerifier:
    """``S256CodeChallenge.validate_code_verifier`` at the token endpoint."""

    @staticmethod
    def _grant(
        *,
        verifier: str | None,
        challenge: str | None,
        method: str | None,
        auth_method: str = "none",
    ) -> MagicMock:
        grant = MagicMock()
        grant.request.form = {} if verifier is None else {"code_verifier": verifier}
        grant.request.auth_method = auth_method
        grant.request.authorization_code = SimpleNamespace(
            code_challenge=challenge, code_challenge_method=method
        )
        return grant

    def test_matching_verifier_passes(self) -> None:
        verifier = secrets.token_urlsafe(48)
        grant = self._grant(
            verifier=verifier, challenge=create_s256_code_challenge(verifier), method="S256"
        )
        S256CodeChallenge(required=True).validate_code_verifier(grant, None)

    def test_wrong_verifier_is_invalid_grant(self) -> None:
        grant = self._grant(
            verifier=secrets.token_urlsafe(48),
            challenge=create_s256_code_challenge(secrets.token_urlsafe(48)),
            method="S256",
        )
        with pytest.raises(InvalidGrantError):
            S256CodeChallenge(required=True).validate_code_verifier(grant, None)

    @pytest.mark.parametrize("method", ["plain", None])
    def test_code_stored_with_another_method_is_invalid_grant(self, method: str | None) -> None:
        verifier = secrets.token_urlsafe(48)
        grant = self._grant(verifier=verifier, challenge=verifier, method=method)
        with pytest.raises(InvalidGrantError):
            S256CodeChallenge(required=True).validate_code_verifier(grant, None)

    def test_public_client_without_verifier_is_invalid_request(self) -> None:
        grant = self._grant(verifier=None, challenge=None, method=None)
        with pytest.raises(InvalidRequestError, match="code_verifier"):
            S256CodeChallenge(required=True).validate_code_verifier(grant, None)

    def test_stored_challenge_needs_a_verifier(self) -> None:
        grant = self._grant(
            verifier=None,
            challenge=_CHALLENGE,
            method="S256",
            auth_method="client_secret_post",
        )
        with pytest.raises(InvalidRequestError, match="code_verifier"):
            S256CodeChallenge(required=True).validate_code_verifier(grant, None)

    def test_confidential_client_without_pkce_passes(self) -> None:
        grant = self._grant(
            verifier=None, challenge=None, method=None, auth_method="client_secret_post"
        )
        S256CodeChallenge(required=True).validate_code_verifier(grant, None)

    def test_malformed_verifier_is_invalid_request(self) -> None:
        grant = self._grant(verifier="short", challenge=_CHALLENGE, method="S256")
        with pytest.raises(InvalidRequestError, match="Invalid 'code_verifier'"):
            S256CodeChallenge(required=True).validate_code_verifier(grant, None)

    def test_registered_on_the_authorization_code_grant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "get_settings", lambda: SimpleNamespace(oauth_pkce_required=True))
        wrapper = OAuth2AuthorizationServer.__new__(OAuth2AuthorizationServer)
        wrapper.server = MagicMock()

        wrapper._register_grants()

        (call,) = [
            c
            for c in wrapper.server.register_grant.call_args_list
            if c.args[0] is AuthorizationCodeGrant
        ]
        (extension,) = call.args[1]
        assert isinstance(extension, S256CodeChallenge)
        assert extension.required is True
        assert extension.SUPPORTED_CODE_CHALLENGE_METHOD == ["S256"]


# ---------------------------------------------------------------------------
# validate_authorization_parameters (the checks before the consent page)
# ---------------------------------------------------------------------------


class TestValidateAuthorizationParameters:
    @staticmethod
    def _settings(monkeypatch: pytest.MonkeyPatch, pkce_required: bool) -> None:
        monkeypatch.setattr(
            mod, "get_settings", lambda: SimpleNamespace(oauth_pkce_required=pkce_required)
        )

    def test_returns_the_granted_scope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._settings(monkeypatch, True)
        payload = _payload(
            scope="memory:read memory:admin undefined:scope",
            code_challenge=_CHALLENGE,
            code_challenge_method="S256",
            resource=MCP,
        )
        assert validate_authorization_parameters(_client(), payload) == "memory:read"

    def test_nothing_grantable_is_invalid_scope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._settings(monkeypatch, True)
        payload = _payload(
            scope="undefined:scope", code_challenge=_CHALLENGE, code_challenge_method="S256"
        )
        with pytest.raises(InvalidScopeError):
            validate_authorization_parameters(_client(), payload)

    def test_pkce_rules_apply_when_enforced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._settings(monkeypatch, True)
        payload = _payload(code_challenge=_CHALLENGE, code_challenge_method="plain")
        with pytest.raises(InvalidRequestError):
            validate_authorization_parameters(_client(), payload)

    def test_pkce_rules_follow_the_kill_switch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # With oauth_pkce_required off the grant registers no PKCE extension,
        # so the consent page does not apply the PKCE rules either.
        self._settings(monkeypatch, False)
        payload = _payload(code_challenge=_CHALLENGE, code_challenge_method="plain")
        assert validate_authorization_parameters(_client(), payload) == DCR_DEFAULT_SCOPE

    def test_foreign_resource_is_invalid_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._settings(monkeypatch, True)
        payload = _payload(
            code_challenge=_CHALLENGE,
            code_challenge_method="S256",
            resource="https://other.example/mcp",
        )
        with pytest.raises(InvalidTargetError):
            validate_authorization_parameters(_client(), payload)
