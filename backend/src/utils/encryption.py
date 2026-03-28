"""Encryption utilities for sensitive data.

Provides Fernet symmetric encryption for API keys and other sensitive values.
Used for storing external API keys (OpenAI, Anthropic, etc.) in database.

Security:
- Fernet provides authenticated encryption (AES-128 in CBC mode + HMAC)
- Encryption key derived from API_KEY_SECRET environment variable
- Encrypted values are base64-encoded strings
- Decryption verifies integrity (prevents tampering)

Example:
    >>> encryptor = get_encryptor()
    >>> encrypted = encryptor.encrypt("sk-proj-abc123...")
    >>> encrypted
    'gAAAAABh...'
    >>> decrypted = encryptor.decrypt(encrypted)
    >>> decrypted
    'sk-proj-abc123...'
"""

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


class APIKeyEncryption:
    """Fernet-based symmetric encryption for API keys.

    Uses API_KEY_SECRET environment variable as the encryption key.
    Provides encrypt/decrypt methods with error handling.
    """

    def __init__(self, secret_key: str):
        """Initialize encryption with secret key.

        Args:
            secret_key: Base64-encoded Fernet key or raw secret (will be hashed to 32 bytes)

        Raises:
            ValueError: If secret_key is empty or invalid
        """
        if not secret_key:
            msg = "Encryption secret key is required"
            raise ValueError(msg)

        # Check if it's already a valid Fernet key (base64-encoded 32-byte key)
        try:
            # Try to use as-is first (if it's already a Fernet key)
            self.cipher = Fernet(secret_key.encode() if isinstance(secret_key, str) else secret_key)
        except Exception:
            # If not valid Fernet key, derive one from the secret using SHA256
            # This allows using any string as API_KEY_SECRET
            key_hash = hashlib.sha256(secret_key.encode()).digest()
            fernet_key = base64.urlsafe_b64encode(key_hash)
            self.cipher = Fernet(fernet_key)

        logger.debug("APIKeyEncryption initialized successfully")

    def encrypt(self, plaintext: str) -> str:
        """Encrypt plaintext value.

        Args:
            plaintext: Plaintext string to encrypt (e.g., API key)

        Returns:
            Base64-encoded encrypted string

        Raises:
            ValueError: If plaintext is empty
            Exception: On encryption failure

        Example:
            >>> encrypted = encryptor.encrypt("sk-proj-abc123")
            >>> encrypted
            'gAAAAABh9X...'
        """
        if not plaintext:
            msg = "Cannot encrypt empty value"
            raise ValueError(msg)

        try:
            encrypted_bytes = self.cipher.encrypt(plaintext.encode())
            return encrypted_bytes.decode()
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            raise

    def decrypt(self, encrypted: str) -> str:
        """Decrypt encrypted value.

        Args:
            encrypted: Base64-encoded encrypted string

        Returns:
            Decrypted plaintext string

        Raises:
            ValueError: If encrypted is empty
            InvalidToken: If encrypted value is invalid or tampered
            Exception: On decryption failure

        Example:
            >>> decrypted = encryptor.decrypt("gAAAAABh9X...")
            >>> decrypted
            'sk-proj-abc123'
        """
        if not encrypted:
            msg = "Cannot decrypt empty value"
            raise ValueError(msg)

        try:
            decrypted_bytes = self.cipher.decrypt(encrypted.encode())
            return decrypted_bytes.decode()
        except InvalidToken as e:
            logger.error(f"Decryption failed: Invalid token (tampered or wrong key): {e}")
            raise ValueError("Invalid encrypted value or wrong encryption key") from e
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            raise

    def mask_value(self, plaintext: str, show_chars: int = 8) -> str:
        """Mask sensitive value for display.

        DEPRECATED: Use utils.masking.mask_prefix_only() instead.

        Args:
            plaintext: Plaintext value to mask
            show_chars: Number of characters to show at start (default: 8)

        Returns:
            Masked value (e.g., "sk-proj-***")

        Example:
            >>> encryptor.mask_value("sk-proj-abc123def456")
            'sk-proj-***'
        """
        from utils.masking import mask_prefix_only

        return mask_prefix_only(plaintext, show_chars)


# Singleton instance
_encryptor: APIKeyEncryption | None = None


def get_encryptor() -> APIKeyEncryption:
    """Get global encryption instance.

    Uses API_KEY_SECRET environment variable.

    Returns:
        APIKeyEncryption instance

    Raises:
        ValueError: If API_KEY_SECRET not set

    Example:
        >>> encryptor = get_encryptor()
        >>> encrypted = encryptor.encrypt("my-api-key")
    """
    global _encryptor

    if _encryptor is None:
        secret = os.getenv("API_KEY_SECRET")
        if not secret:
            msg = "API_KEY_SECRET environment variable not set. Required for API key encryption."
            raise ValueError(msg)

        _encryptor = APIKeyEncryption(secret)
        logger.info("Global encryption instance initialized")

    return _encryptor


def generate_fernet_key() -> str:
    """Generate a new Fernet key for API_KEY_SECRET.

    Returns:
        Base64-encoded Fernet key (44 characters)

    Example:
        >>> key = generate_fernet_key()
        >>> len(key)
        44
        >>> key[:10]
        'QXR5cGljYW...'

    Note:
        Store this in .env.cloud as API_KEY_SECRET.
        Keep it secret and never commit to git.
    """
    return Fernet.generate_key().decode()
