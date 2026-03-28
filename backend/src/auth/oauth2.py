"""OAuth2 authentication manager for Google services"""

import json
import logging
import os
from datetime import UTC
from typing import Any

from cryptography.fernet import Fernet
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow, InstalledAppFlow

from auth.config import AuthConfig
from auth.exceptions import (
    InvalidCredentialsError,
    NotAuthenticatedError,
    TokenRefreshError,
)

logger = logging.getLogger(__name__)


class OAuth2Manager:
    """OAuth2 authentication manager for Google services

    Handles OAuth2 authentication flow, token management, and secure credential storage.

    Args:
        provider: OAuth2 provider name (default: "google")
        config: Optional AuthConfig instance

    Example:
        >>> auth = OAuth2Manager(provider="google")
        >>> auth.login()  # Opens browser for authentication
        >>> creds = auth.get_credentials()  # Returns valid credentials
        >>> token = auth.get_token()  # Returns access token

    Security:
        - Credentials are encrypted using Fernet (AES-128)
        - Encryption key stored separately with 0o600 permissions
        - Credentials file has 0o600 permissions
        - Automatic token refresh when expired
    """

    SCOPES = {
        "google": [
            "https://www.googleapis.com/auth/generative-language",  # For Gemini API (CLI only)
            "openid",
        ]
    }

    # Web-specific scopes (for web dashboard login)
    WEB_SCOPES = {
        "google": [
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
        ]
    }

    def __init__(self, provider: str = "google", config: AuthConfig | None = None):
        """Initialize OAuth2 manager

        Args:
            provider: OAuth2 provider name
            config: Optional authentication configuration
        """
        self.provider = provider
        self.config = config or AuthConfig(provider=provider)

        # Setup configuration directory
        from config.paths import get_config_dir

        self.config_dir = get_config_dir()
        self.config_dir.mkdir(exist_ok=True, parents=True)

        # File paths
        self.creds_file = self.config_dir / "credentials.json.enc"
        self.key_file = self.config_dir / ".key"
        self.client_secrets_file = (
            self.config.client_secrets_path or self.config_dir / "client_secrets.json"
        )

        # Setup encryption
        self._setup_encryption()

        logger.debug(f"Initialized OAuth2Manager for {provider} (config_dir: {self.config_dir})")

    def _setup_encryption(self) -> None:
        """Setup encryption key for credential storage"""
        if not self.key_file.exists():
            logger.info("Generating new encryption key")
            key = Fernet.generate_key()
            self.key_file.write_bytes(key)
            self.key_file.chmod(0o600)
            logger.debug(f"Encryption key saved to {self.key_file}")

        self.cipher = Fernet(self.key_file.read_bytes())

    def login(self) -> None:
        """Launch browser for OAuth2 authentication

        Raises:
            FileNotFoundError: If client_secrets.json not found
            InvalidCredentialsError: If authentication fails
        """
        if not self.client_secrets_file.exists():
            raise FileNotFoundError(
                f"Client secrets file not found: {self.client_secrets_file}\n\n"
                "Please download client_secrets.json from Google Cloud Console:\n"
                "1. Go to https://console.cloud.google.com/apis/credentials\n"
                "2. Create OAuth 2.0 Client ID (Desktop application)\n"
                "3. Download JSON and save as: {self.client_secrets_file}"
            )

        try:
            logger.info(f"Starting OAuth2 flow for {self.provider}")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.client_secrets_file),
                self.config.scopes or self.SCOPES[self.provider],
            )

            # Run local server for OAuth2 callback
            creds_result = flow.run_local_server(port=0)

            # Ensure we have OAuth2 Credentials (not ExternalAccountCredentials)
            if not isinstance(creds_result, Credentials):
                raise InvalidCredentialsError(
                    "Received unexpected credential type from OAuth2 flow"
                )

            # Save encrypted credentials
            self._save_credentials(creds_result)

            logger.info("Authentication successful")
            print("✓ Authentication successful!")
            print(f"✓ Credentials saved to: {self.creds_file}")

        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            raise InvalidCredentialsError(f"Authentication failed: {e}") from e

    def logout(self) -> None:
        """Remove stored credentials

        Raises:
            NotAuthenticatedError: If not authenticated
        """
        if not self.is_authenticated():
            raise NotAuthenticatedError(self.provider)

        logger.info(f"Logging out from {self.provider}")
        self.creds_file.unlink()
        print(f"✓ Logged out from {self.provider}")

    def is_authenticated(self) -> bool:
        """Check if user is authenticated

        Returns:
            True if valid credentials exist
        """
        return self.creds_file.exists()

    def get_credentials(self) -> Credentials:
        """Get valid credentials with automatic refresh

        Returns:
            Valid Google OAuth2 credentials

        Raises:
            NotAuthenticatedError: If not authenticated
            TokenRefreshError: If token refresh fails
        """
        if not self.is_authenticated():
            raise NotAuthenticatedError(self.provider)

        # Load credentials
        creds = self._load_credentials()

        # Refresh if expired
        if hasattr(creds, "expired") and creds.expired and creds.refresh_token:
            try:
                logger.info("Refreshing expired token")
                creds.refresh(Request())
                self._save_credentials(creds)
                logger.debug("Token refreshed successfully")
            except Exception as e:
                logger.error(f"Token refresh failed: {e}")
                raise TokenRefreshError(self.provider, reason=str(e)) from e

        return creds

    def get_token(self) -> str:
        """Get access token for API calls

        Returns:
            Access token string

        Raises:
            NotAuthenticatedError: If not authenticated
            TokenRefreshError: If token refresh fails
        """
        creds = self.get_credentials()
        if not creds.token:
            raise InvalidCredentialsError("No access token available")
        return creds.token

    def _save_credentials(self, creds: Credentials) -> None:
        """Encrypt and save credentials

        Args:
            creds: Google OAuth2 credentials to save
        """
        # Prepare credentials data for encryption
        creds_data = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes,
        }

        # Save expiry as ISO format string with timezone
        if creds.expiry:
            # Ensure expiry has timezone info before saving

            expiry_to_save = creds.expiry
            if expiry_to_save.tzinfo is None:
                # If naive, assume UTC
                expiry_to_save = expiry_to_save.replace(tzinfo=UTC)
            creds_data["expiry"] = expiry_to_save.isoformat()
        else:
            creds_data["expiry"] = None

        # Encrypt credentials
        encrypted = self.cipher.encrypt(json.dumps(creds_data).encode())

        # Save with secure permissions
        self.creds_file.write_bytes(encrypted)
        self.creds_file.chmod(0o600)

        logger.debug(f"Credentials saved to {self.creds_file}")

    def _load_credentials(self) -> Credentials:
        """Decrypt and load credentials

        Returns:
            Google OAuth2 credentials

        Raises:
            InvalidCredentialsError: If decryption fails
        """
        try:
            # Decrypt credentials
            encrypted = self.creds_file.read_bytes()
            decrypted = self.cipher.decrypt(encrypted)
            creds_data = json.loads(decrypted)

            # Extract expiry separately (not part of from_authorized_user_info)
            expiry_str = creds_data.pop("expiry", None)

            # Reconstruct Credentials object
            creds = Credentials.from_authorized_user_info(
                creds_data, self.config.scopes or self.SCOPES[self.provider]
            )

            # Restore expiry if it exists
            if expiry_str:
                from datetime import datetime

                # Parse ISO format datetime (preserves timezone from string)
                expiry_dt = datetime.fromisoformat(expiry_str)
                # IMPORTANT: Google auth library's _helpers.utcnow() is timezone-NAIVE
                # We must store expiry as naive UTC datetime to match
                if expiry_dt.tzinfo is not None:
                    # Convert to UTC and make naive
                    expiry_dt = expiry_dt.astimezone(UTC).replace(tzinfo=None)
                # Now expiry_dt is naive UTC datetime, matching Google's expectations
                creds.expiry = expiry_dt

            return creds

        except Exception as e:
            logger.error(f"Failed to load credentials: {e}")
            raise InvalidCredentialsError(f"Failed to decrypt credentials: {e}") from e

    # ========================================================================
    # Web OAuth2 Flow (Issue #650)
    # ========================================================================

    def get_authorization_url_web(self, redirect_uri: str, state: str) -> str:
        """Get OAuth2 authorization URL for Web flow.

        Args:
            redirect_uri: Callback URL (e.g., https://your-domain.com/auth/callback)
            state: CSRF state token

        Returns:
            Authorization URL to redirect user to

        Example:
            >>> manager = OAuth2Manager()
            >>> url = manager.get_authorization_url_web(
            ...     redirect_uri="https://your-domain.com/auth/callback",
            ...     state="random_csrf_token"
            ... )
        """
        client_id = os.getenv("GOOGLE_CLIENT_ID")
        if not client_id:
            raise ValueError("GOOGLE_CLIENT_ID environment variable not set")

        # Use web-specific scopes (openid, email, profile)
        scopes = self.WEB_SCOPES[self.provider]
        scope_str = " ".join(scopes)

        auth_url = (
            f"https://accounts.google.com/o/oauth2/v2/auth?"
            f"client_id={client_id}&"
            f"redirect_uri={redirect_uri}&"
            f"response_type=code&"
            f"scope={scope_str}&"
            f"state={state}&"
            f"access_type=offline&"
            f"prompt=consent"
        )

        return auth_url

    def exchange_code_web(self, code: str, redirect_uri: str) -> Credentials:
        """Exchange authorization code for credentials (Web flow).

        Args:
            code: Authorization code from Google
            redirect_uri: Same redirect_uri used in authorization

        Returns:
            Google OAuth2 credentials

        Raises:
            InvalidCredentialsError: If code exchange fails
        """
        client_id = os.getenv("GOOGLE_CLIENT_ID")
        client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

        if not client_id or not client_secret:
            raise ValueError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET required")

        try:
            client_config = {
                "web": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [redirect_uri],
                }
            }

            flow = Flow.from_client_config(
                client_config,
                scopes=self.WEB_SCOPES[self.provider],
                redirect_uri=redirect_uri,
            )

            flow.fetch_token(code=code)
            logger.info("Code exchange successful")
            return flow.credentials

        except Exception as e:
            logger.error(f"Code exchange failed: {e}")
            raise InvalidCredentialsError(f"Code exchange failed: {e}") from e

    def get_user_info_web(self, credentials: Credentials) -> dict[str, Any]:
        """Get user info from Google.

        Args:
            credentials: Google OAuth2 credentials

        Returns:
            User info dict with sub, email, name, picture
        """
        import requests

        headers = {"Authorization": f"Bearer {credentials.token}"}
        response = requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers=headers,
        )
        response.raise_for_status()

        return response.json()
