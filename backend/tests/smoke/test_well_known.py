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
