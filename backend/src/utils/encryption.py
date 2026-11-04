"""Encryption utilities for sensitive data.

Provides Fernet symmetric encryption for API keys and other sensitive values.
Used for storing external API keys (OpenAI, Anthropic, etc.) in database.

Security:
- Fernet provides authenticated encryption (AES-128 in CBC mode + HMAC)
- Encryption key derived from API_KEY_SECRET (read through ``Settings``)
- Encrypted values are base64-encoded strings
- Decryption verifies integrity (prevents tampering)

Key rotation (#2394): the encryptor is a key ring (``MultiFernet``).
``API_KEY_SECRET`` is the current key, used for every new value; the keys in
``API_KEY_SECRET_PREVIOUS`` are still accepted for decryption. The ciphertext
format is unchanged (a Fernet token carries no key id: decryption tries each
key in turn), so values written before a rotation need no migration.
``python -m src.cli.rekey_encrypted_secrets`` re-encrypts the stored values
under the current key, after which the previous keys can be dropped.

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
from collections.abc import Iterable

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from config.settings import get_settings, is_placeholder_secret

logger = logging.getLogger(__name__)


_DECRYPT_ERROR = "Invalid encrypted value or wrong encryption key"


def derive_fernet_key(secret: str) -> str:
    """The Fernet key a configured secret stands for (#2394).

    A base64-encoded 32-byte Fernet key is used as is (returned in its
    canonical form, so a canonical key comes back unchanged); any other
    string is hashed with SHA-256 into one, so ``API_KEY_SECRET`` can be any
    string. The result opens exactly what ``secret`` opens and holds no comma
    or blank, so it can be listed in ``API_KEY_SECRET_PREVIOUS`` where a
    secret with either cannot be listed verbatim. It is as secret as the key.
    """
    try:
        Fernet(secret.encode())
    except Exception:
        key_hash = hashlib.sha256(secret.encode()).digest()
        return base64.urlsafe_b64encode(key_hash).decode()
    # Fernet decodes leniently (characters outside the alphabet are dropped):
    # re-encode the 32 bytes it uses.
    return base64.urlsafe_b64encode(base64.urlsafe_b64decode(secret.encode())).decode()


def _fernet(secret: str) -> Fernet:
    """The Fernet for one configured key (see :func:`derive_fernet_key`)."""
    return Fernet(derive_fernet_key(secret).encode())


class APIKeyEncryption:
    """Fernet-based symmetric encryption for API keys, over a key ring.

    The caller passes the keys; ``get_encryptor`` passes ``API_KEY_SECRET``
    and ``API_KEY_SECRET_PREVIOUS``. New values are encrypted with the current
    key; a value under any key of the ring decrypts (#2394).
    """

    def __init__(self, secret_key: str, previous_keys: Iterable[str] = ()):
        """Initialize encryption with the current key and the older ones.

        Args:
            secret_key: The current key: a base64-encoded Fernet key or a raw
                secret (hashed to 32 bytes).
            previous_keys: Older keys, accepted for decryption only, derived
                the same way. Surrounding blanks are stripped; blank entries
                and keys already in the ring (the current one included, in
                either spelling: the secret or its derived Fernet key) are
                ignored.

        Raises:
            ValueError: If secret_key is empty
        """
        if not secret_key:
            msg = "Encryption secret key is required"
            raise ValueError(msg)

        self._current = _fernet(secret_key)
        ring = [self._current]
        seen = {derive_fernet_key(secret_key)}
        for key in previous_keys:
            key = key.strip()
            if not key:
                continue
            derived = derive_fernet_key(key)
            if derived not in seen:
                seen.add(derived)
                ring.append(Fernet(derived.encode()))
        # MultiFernet encrypts with the first key and decrypts with any.
        self.cipher = MultiFernet(ring)
        self._key_count = len(ring)

        logger.debug("APIKeyEncryption initialized successfully")

    @property
    def key_count(self) -> int:
        """How many distinct keys the ring holds (the current one included)."""
        return self._key_count

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
            encrypted_bytes = self._current.encrypt(plaintext.encode())
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
            raise ValueError(_DECRYPT_ERROR) from e
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            raise

    def is_current(self, encrypted: str) -> bool:
        """Whether the current key opens ``encrypted`` (#2394).

        Args:
            encrypted: Base64-encoded encrypted string

        Returns:
            True when the current key opens it, False when only a previous
            key does.

        Raises:
            ValueError: ``encrypted`` is empty, or no key of the ring opens it
                (the same errors as :meth:`decrypt`).
        """
        if not encrypted:
            msg = "Cannot decrypt empty value"
            raise ValueError(msg)
        token = encrypted.encode()
        try:
            self._current.decrypt(token)
            return True
        except InvalidToken:
            pass
        try:
            self.cipher.decrypt(token)
        except InvalidToken as e:
            raise ValueError(_DECRYPT_ERROR) from e
        return False

    def rotate(self, encrypted: str) -> str:
        """Re-encrypt ``encrypted`` under the current key (#2394).

        The plaintext is unchanged; the token is new (and different from the
        input even when the current key already opened it).

        Args:
            encrypted: Base64-encoded encrypted string under any key of the ring

        Returns:
            Base64-encoded encrypted string under the current key

        Raises:
            ValueError: ``encrypted`` is empty, or no key of the ring opens it
        """
        if not encrypted:
            msg = "Cannot decrypt empty value"
            raise ValueError(msg)
        try:
            return self.cipher.rotate(encrypted.encode()).decode()
        except InvalidToken as e:
            raise ValueError(_DECRYPT_ERROR) from e

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

    Uses ``Settings.api_key_secret`` (``API_KEY_SECRET``), so the key is the
    one the production secret guard checked (#2239). Outside production an
    unset ``API_KEY_SECRET`` falls back to the public development default; that
    or any shipped placeholder logs a warning. Production refuses them when the
    settings load. ``API_KEY_SECRET_PREVIOUS`` adds the older keys of a
    rotation, for decryption only (#2394).

    Returns:
        APIKeyEncryption instance

    Raises:
        ValueError: If API_KEY_SECRET is empty

    Example:
        >>> encryptor = get_encryptor()
        >>> encrypted = encryptor.encrypt("my-api-key")
    """
    global _encryptor

    if _encryptor is None:
        settings = get_settings()
        secret = settings.api_key_secret
        if not secret.strip():
            msg = "API_KEY_SECRET is empty. Required for API key encryption."
            raise ValueError(msg)
        if is_placeholder_secret(secret):
            # Once per process: the singleton is built once.
            logger.warning(
                "API_KEY_SECRET is a public development default; stored provider "
                "keys and connector secrets are encrypted with a key anyone can read. "
                "Set a private value (python -m src.cli.setup_env, or openssl rand -hex 32)."
            )

        _encryptor = APIKeyEncryption(secret, settings.api_key_secret_previous_keys)
        logger.info(
            "Global encryption instance initialized (%d previous key(s))",
            _encryptor.key_count - 1,
        )

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
