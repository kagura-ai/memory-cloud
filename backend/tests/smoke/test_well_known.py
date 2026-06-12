"""Smoke tests for .well-known OAuth2 metadata endpoints.

These endpoints must be publicly accessible (no auth required)
for MCP client discovery.
"""

import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture
def client():
    """Create test client without auth overrides."""
    with TestClient(app) as c:
        yield c


class TestWellKnownEndpoints:
    """Test .well-known OAuth2/OIDC metadata endpoints."""

    def test_oauth_protected_resource(self, client):
        """GET /.well-known/oauth-protected-resource returns metadata."""
        response = client.get("/.well-known/oauth-protected-resource")
        assert response.status_code == 200
        data = response.json()
        assert "resource" in data or "issuer" in data

    def test_oauth_protected_resource_honest_fields(self, client):
        """Protected-resource metadata must not advertise misleading optional fields (#993).

        Tokens are opaque (introspection-validated, never signed), so a signing-alg
        list would invite a doomed signature check; and there is no published policy
        document, so resource_policy_uri (which used to point at the Swagger UI) is
        dropped. The Kagura mcp_sse_endpoint extension stays as a committed surface.
        """
        data = client.get("/.well-known/oauth-protected-resource").json()
        assert "resource_signing_alg_values_supported" not in data
        assert "resource_policy_uri" not in data
        assert "mcp_sse_endpoint" in data

    def test_device_code_grant_advertised(self, client):
        """Both AS-metadata docs advertise the live device-code grant + endpoint (#993).

        The device flow is wired end-to-end; under-advertising it in a frozen-at-1.0
        metadata doc is a compat trap. RFC 8628 §4 requires the device_authorization
        endpoint to accompany the grant so a client can actually start the flow.
        """
        device_grant = "urn:ietf:params:oauth:grant-type:device_code"
        for endpoint in (
            "/.well-known/openid-configuration",
            "/.well-known/oauth-authorization-server",
        ):
            data = client.get(endpoint).json()
            assert device_grant in data["grant_types_supported"], endpoint
            assert data["device_authorization_endpoint"].endswith(
                "/api/v1/oauth/device/authorize"
            ), endpoint

    def test_oauth_protected_resource_mcp(self, client):
        """GET /.well-known/oauth-protected-resource/mcp returns MCP metadata."""
        response = client.get("/.well-known/oauth-protected-resource/mcp")
        assert response.status_code == 200

    def test_openid_configuration(self, client):
        """GET /.well-known/openid-configuration returns OIDC discovery."""
        response = client.get("/.well-known/openid-configuration")
        assert response.status_code == 200
        data = response.json()
        # Should have standard OIDC fields
        assert "issuer" in data or "authorization_endpoint" in data

    def test_oauth_authorization_server(self, client):
        """GET /.well-known/oauth-authorization-server returns AS metadata."""
        response = client.get("/.well-known/oauth-authorization-server")
        assert response.status_code == 200
        data = response.json()
        assert "issuer" in data or "authorization_endpoint" in data
