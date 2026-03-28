"""Authentication configuration for OAuth2 and API keys.

Issue #13: OAuth2 authentication configuration
Issue #31: Frontend integration
"""

from pathlib import Path


class AuthConfig:
    """Authentication configuration.

    Args:
        provider: OAuth2 provider name (default: "google")
        scopes: Optional list of OAuth2 scopes
        client_secrets_path: Optional path to client secrets JSON file
    """

    def __init__(
        self,
        provider: str = "google",
        scopes: list[str] | None = None,
        client_secrets_path: Path | None = None,
    ):
        self.provider = provider
        self.scopes = scopes
        self.client_secrets_path = client_secrets_path
