"""Unit tests for the authorization request rules in ``auth.oauth2_server`` (#1686).

Scope (RFC 6749 §3.3), PKCE with ``S256`` only (RFC 7636) and resource
indicators (RFC 8707), including the shared MCP-resource helper in
``auth.mcp_resource``. The end-to-end behaviour through the endpoints is in
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
from auth.mcp_resource import is_same_mcp_resource, mcp_resource_identifier  # noqa: E402
from auth.mcp_scopes import DCR_DEFAULT_SCOPE  # noqa: E402
from auth.oauth2_server import (  # noqa: E402
    AuthorizationCodeGrant,
    InvalidTargetError,
    OAuth2AuthorizationServer,
    S256CodeChallenge,
    check_code_challenge,
    requested_resource,
    settle_audience,
    validate_authorization_parameters,
)
from auth.oauth_scope import (  # noqa: E402
    client_registered_scope,
    granted_scope,
    registration_scope,
)
from models.auth import OAuth2Client  # noqa: E402

MCP = "https://memory.example.test/mcp"
WORKSPACE_ID = "3f2b8c1e-5d6a-4b7c-9e0f-1a2b3c4d5e6f"
FOREIGN = "https://other.example/mcp"


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


def _client(
    auth_method: str = "none", scope: str = DCR_DEFAULT_SCOPE, owner_id: str | None = None
) -> SimpleNamespace:
    """A client stand-in; ``owner_id=None`` is a DCR registration."""
    return SimpleNamespace(token_endpoint_auth_method=auth_method, scope=scope, owner_id=owner_id)


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
            # No memory scope left: the registered scope is granted.
            ("claudeai", DCR_DEFAULT_SCOPE, DCR_DEFAULT_SCOPE),
            ("openid offline_access", DCR_DEFAULT_SCOPE, DCR_DEFAULT_SCOPE),
            ("openid", "memory:read openid", "memory:read openid"),
            ("undefined:scope", DCR_DEFAULT_SCOPE, DCR_DEFAULT_SCOPE),
            ("memory:admin", DCR_DEFAULT_SCOPE, DCR_DEFAULT_SCOPE),
            ("memory:read", "legacy:scope memory:write", "memory:write"),
            # Nothing grantable: the registration has no memory scope.
            ("memory:read", "", ""),
            ("openid", "openid offline_access", ""),
            (None, "legacy:scope", ""),
        ],
    )
    def test_granted_scope(self, requested: str | None, registered: str, granted: str) -> None:
        assert granted_scope(requested, registered) == granted


class TestRegistrationScope:
    """Scope ``/register`` stores (``registration_scope``)."""

    @pytest.mark.parametrize(
        ("requested", "stored"),
        [
            (None, DCR_DEFAULT_SCOPE),
            ("", DCR_DEFAULT_SCOPE),
            ("memory:read memory:write", "memory:read memory:write"),
            ("memory:read claudeai", "memory:read"),
            ("memory:admin memory:read", "memory:admin memory:read"),
            # No memory scope this server defines: the default scope.
            ("claudeai", DCR_DEFAULT_SCOPE),
            ("openid offline_access", DCR_DEFAULT_SCOPE),
            ("openid profile", DCR_DEFAULT_SCOPE),
            ("offline_access openid", DCR_DEFAULT_SCOPE),
        ],
    )
    def test_registration_scope(self, requested: str | None, stored: str) -> None:
        assert registration_scope(requested) == stored


class TestClientRegisteredScope:
    """The registered scope the rule starts from (``client_registered_scope``)."""

    @pytest.mark.parametrize("stored", ["claudeai", "openid offline_access", "openid profile", ""])
    def test_dcr_client_without_memory_scope_gets_the_default(self, stored: str) -> None:
        assert client_registered_scope(_client(scope=stored)) == DCR_DEFAULT_SCOPE

    def test_dcr_client_with_memory_scope_keeps_it(self) -> None:
        assert client_registered_scope(_client(scope="memory:read claudeai")) == (
            "memory:read claudeai"
        )

    def test_admin_managed_client_keeps_its_scope(self) -> None:
        client = _client(scope="openid offline_access", owner_id="admin-user")
        assert client_registered_scope(client) == "openid offline_access"
        assert granted_scope("openid", client_registered_scope(client)) == ""

    def test_dcr_client_registered_with_claudeai_is_granted_the_default(self) -> None:
        assert granted_scope("claudeai", client_registered_scope(_client(scope="claudeai"))) == (
            DCR_DEFAULT_SCOPE
        )


class TestModelGetAllowedScope:
    """``OAuth2Client.get_allowed_scope``, which Authlib calls while it validates
    an authorization request, grants what the endpoints' scope rule grants."""

    @staticmethod
    def _model(scope: str, owner_id: str | None) -> OAuth2Client:
        return OAuth2Client(
            client_id="oauth_scope_rule",
            client_secret_hash="",
            client_name="Scope Rule Client",
            redirect_uris=["http://localhost:8080/callback"],
            grant_types=["authorization_code"],
            response_types=["code"],
            scope=scope,
            token_endpoint_auth_method="none",
            owner_id=owner_id,
        )

    @pytest.mark.parametrize(
        ("scope", "owner_id"),
        [
            (DCR_DEFAULT_SCOPE, None),
            ("claudeai", None),
            ("memory:read memory:admin", "admin-user"),
            ("openid offline_access", "admin-user"),
        ],
    )
    @pytest.mark.parametrize(
        "requested",
        [None, "", "memory:read", "memory:admin", "claudeai", "openid offline_access", "x y"],
    )
    def test_matches_the_scope_rule(
        self, scope: str, owner_id: str | None, requested: str | None
    ) -> None:
        client = self._model(scope, owner_id)
        expected = granted_scope(requested, client_registered_scope(client)) or None
        assert client.get_allowed_scope(requested) == expected

    def test_nothing_grantable_is_none(self) -> None:
        # Authlib answers invalid_scope for None.
        assert (
            self._model("openid offline_access", "admin-user").get_allowed_scope("openid") is None
        )

    def test_dcr_row_with_claudeai_is_granted_the_default(self) -> None:
        assert self._model("claudeai", None).get_allowed_scope("claudeai") == DCR_DEFAULT_SCOPE


# ---------------------------------------------------------------------------
# resource
# ---------------------------------------------------------------------------


class TestMcpResource:
    """``auth.mcp_resource``: the published identifier and the matching rule."""

    async def test_identifier_is_the_protected_resource_metadata(
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
        "value",
        [
            MCP,
            f"{MCP}/",
            f"{MCP}/w/{WORKSPACE_ID}",
            f"{MCP}/sse",
            f"{MCP}?profile=core",
            f"{MCP}/w/{WORKSPACE_ID}?profile=core&guardrails=off",
            f"{MCP}#fragment",
            "HTTPS://Memory.Example.Test/mcp",
            "https://memory.example.test:443/mcp",
            # One trailing dot on the host, and IDNA-equivalent hosts.
            "https://memory.example.test./mcp",
            "https://ｍｅｍｏｒｙ.example.test/mcp",
        ],
    )
    def test_names_the_mcp_resource(self, value: str) -> None:
        assert is_same_mcp_resource(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "http://memory.example.test/mcp",
            "https://memory.example.test:8443/mcp",
            FOREIGN,
            "https://memory.example.test.other.example/mcp",
            "https://memory.example.test",
            "https://memory.example.test/",
            "https://memory.example.test/mcpx",
            "https://memory.example.test/api/v1/memory",
            "https://user@memory.example.test/mcp",
            f"{MCP}/../api",
            f"{MCP}/%2e%2e/api",
            "/mcp",
            "not a url",
            "",
            "http://[::1",
            "https://memory.example.test:port/mcp",
            # Another host: an empty label is not dropped.
            "https://memory..example.test/mcp",
            # Two trailing dots is a different host: only one is dropped.
            "https://memory.example.test../mcp",
        ],
    )
    def test_does_not_name_the_mcp_resource(self, value: str) -> None:
        assert is_same_mcp_resource(value) is False

    def test_default_port_in_the_published_identifier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FRONTEND_URL", "https://memory.example.test:443")
        assert is_same_mcp_resource(MCP) is True

    def test_published_host_is_normalised_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FRONTEND_URL", "https://Bücher.example.")
        assert is_same_mcp_resource("https://xn--bcher-kva.example/mcp") is True
        assert is_same_mcp_resource("https://bücher.example/mcp/w/x") is True
        assert is_same_mcp_resource("https://buecher.example/mcp") is False

    @pytest.mark.parametrize(
        "host",
        [
            # An empty label and a label longer than 63 characters are not
            # valid IDNA names. The same text on both sides would match
            # without the IDNA check.
            "memory..example.test",
            f"{'a' * 64}.example.test",
        ],
    )
    def test_invalid_idna_host_matches_nothing(
        self, monkeypatch: pytest.MonkeyPatch, host: str
    ) -> None:
        monkeypatch.setenv("FRONTEND_URL", f"https://{host}")
        assert is_same_mcp_resource(f"https://{host}/mcp") is False

    def test_explicit_port_and_base_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FRONTEND_URL", "http://127.0.0.1:8080")
        monkeypatch.setenv("MCP_BASE_PATH", "/custom-mcp")
        assert is_same_mcp_resource("http://127.0.0.1:8080/custom-mcp/w/x") is True
        assert is_same_mcp_resource("http://127.0.0.1/custom-mcp") is False
        assert is_same_mcp_resource("http://127.0.0.1:8080/mcp") is False


class TestRequestedResource:
    def test_absent_or_empty_resource(self) -> None:
        assert requested_resource(_payload()) is None
        assert requested_resource(_payload(resource="")) is None

    @pytest.mark.parametrize(
        "value",
        [MCP, f"{MCP}/", f"{MCP}/w/{WORKSPACE_ID}", f"{MCP}?profile=core", f"{MCP}/sse"],
    )
    def test_mcp_resource_is_returned_as_published(self, value: str) -> None:
        assert requested_resource(_payload(resource=value)) == MCP

    @pytest.mark.parametrize("value", [FOREIGN, "https://memory.example.test/api", "not a url"])
    def test_other_resource_is_invalid_target(self, value: str) -> None:
        with pytest.raises(InvalidTargetError) as excinfo:
            requested_resource(_payload(resource=value))
        assert excinfo.value.error == "invalid_target"

    def test_every_value_must_name_the_mcp_resource(self) -> None:
        assert requested_resource(_multi_payload(resource=[MCP, f"{MCP}/w/x"])) == MCP
        with pytest.raises(InvalidTargetError):
            requested_resource(_multi_payload(resource=[MCP, FOREIGN]))


class TestSettleAudience:
    """Audience of a token from a code or a refresh token (``settle_audience``)."""

    @pytest.mark.parametrize(
        ("requested", "bound", "audience"),
        [
            (None, None, None),
            (None, "", None),
            (MCP, None, MCP),
            (None, MCP, MCP),
            (MCP, MCP, MCP),
            # A bound audience stored in another accepted form is normalised.
            (None, f"{MCP}/w/{WORKSPACE_ID}", MCP),
            (MCP, f"{MCP}/w/{WORKSPACE_ID}", MCP),
            (None, f"{MCP}?profile=core", MCP),
            (MCP, f"{MCP}/", MCP),
            # A bound foreign audience is kept when the request names none.
            (None, FOREIGN, FOREIGN),
        ],
    )
    def test_audience(self, requested: str | None, bound: str | None, audience: str | None) -> None:
        assert settle_audience(requested, bound) == audience

    def test_request_naming_the_mcp_resource_against_a_foreign_audience(self) -> None:
        with pytest.raises(InvalidTargetError):
            settle_audience(MCP, FOREIGN)


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

    def test_verifier_for_a_code_without_challenge_is_invalid_request(self) -> None:
        # RFC 9700 §4.8, as Authlib's CodeChallenge does from 1.8.
        grant = self._grant(
            verifier=secrets.token_urlsafe(48),
            challenge=None,
            method=None,
            auth_method="client_secret_post",
        )
        with pytest.raises(InvalidRequestError, match="no 'code_challenge'"):
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

    def test_no_memory_scope_requested_gets_the_registered_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._settings(monkeypatch, True)
        payload = _payload(
            scope="claudeai", code_challenge=_CHALLENGE, code_challenge_method="S256"
        )
        assert validate_authorization_parameters(_client(), payload) == DCR_DEFAULT_SCOPE

    def test_dcr_registration_without_memory_scope_gets_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._settings(monkeypatch, True)
        payload = _payload(
            scope="claudeai", code_challenge=_CHALLENGE, code_challenge_method="S256"
        )
        assert validate_authorization_parameters(_client(scope="claudeai"), payload) == (
            DCR_DEFAULT_SCOPE
        )

    def test_admin_client_without_memory_scope_is_invalid_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._settings(monkeypatch, True)
        payload = _payload(scope="openid", code_challenge=_CHALLENGE, code_challenge_method="S256")
        with pytest.raises(InvalidScopeError):
            validate_authorization_parameters(
                _client(scope="openid offline_access", owner_id="admin-user"), payload
            )

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
            resource=FOREIGN,
        )
        with pytest.raises(InvalidTargetError):
            validate_authorization_parameters(_client(), payload)
