"""Tests for OAuth provider discovery endpoint.

Issue #360: AUTH_PROVIDERS env var control.
"""

import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


class TestAuthProviders:
    """Test GET /api/v1/auth/providers."""

    def test_providers_endpoint_returns_200(self):
        """Endpoint is public and returns 200."""
        response = client.get("/api/v1/auth/providers")
        assert response.status_code == 200
        assert "providers" in response.json()

    def test_providers_returns_list(self):
        """Response contains providers list with name field."""
        response = client.get("/api/v1/auth/providers")
        data = response.json()
        assert isinstance(data["providers"], list)
        for p in data["providers"]:
            assert "name" in p

    @patch.dict(
        os.environ,
        {
            "AUTH_PROVIDERS": "google",
            "GOOGLE_CLIENT_ID": "test-id",
            "GITHUB_CLIENT_ID": "test-id",
        },
    )
    def test_explicit_google_only(self):
        """AUTH_PROVIDERS=google: only Google shown."""
        response = client.get("/api/v1/auth/providers")
        providers = [p["name"] for p in response.json()["providers"]]
        assert providers == ["google"]

    @patch.dict(
        os.environ,
        {
            "AUTH_PROVIDERS": "github",
            "GOOGLE_CLIENT_ID": "test-id",
            "GITHUB_CLIENT_ID": "test-id",
        },
    )
    def test_explicit_github_only(self):
        """AUTH_PROVIDERS=github: only GitHub shown."""
        response = client.get("/api/v1/auth/providers")
        providers = [p["name"] for p in response.json()["providers"]]
        assert providers == ["github"]

    @patch.dict(
        os.environ,
        {
            "AUTH_PROVIDERS": "google,github",
            "GOOGLE_CLIENT_ID": "test-id",
            "GITHUB_CLIENT_ID": "test-id",
        },
    )
    def test_explicit_both(self):
        """AUTH_PROVIDERS=google,github: both shown."""
        response = client.get("/api/v1/auth/providers")
        providers = [p["name"] for p in response.json()["providers"]]
        assert "google" in providers
        assert "github" in providers
        assert len(providers) == 2

    @patch.dict(
        os.environ,
        {
            "AUTH_PROVIDERS": "auto",
            "GOOGLE_CLIENT_ID": "",
            "GITHUB_CLIENT_ID": "",
        },
    )
    def test_no_providers_configured(self):
        """No credentials: empty list."""
        response = client.get("/api/v1/auth/providers")
        assert response.json()["providers"] == []
