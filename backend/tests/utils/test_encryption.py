"""Tests for utils/encryption.py.

Pure utility — no DB or external services required.
"""

import base64
from unittest.mock import patch

import pytest

from utils.encryption import APIKeyEncryption, generate_fernet_key, get_encryptor


class TestAPIKeyEncryption:
    def test_init_with_valid_fernet_key(self):
        key = generate_fernet_key()
        encryptor = APIKeyEncryption(key)
        assert encryptor is not None

    def test_init_with_raw_string_derives_key(self):
        encryptor = APIKeyEncryption("my-secret-password-123")
        assert encryptor is not None

    def test_init_empty_secret_raises(self):
        with pytest.raises(ValueError, match="Encryption secret key is required"):
            APIKeyEncryption("")

    def test_encrypt_decrypt_roundtrip(self):
        key = generate_fernet_key()
        encryptor = APIKeyEncryption(key)
        plaintext = "sk-proj-abc123-secret"
        encrypted = encryptor.encrypt(plaintext)
        assert encrypted != plaintext
        decrypted = encryptor.decrypt(encrypted)
        assert decrypted == plaintext

    def test_encrypt_empty_raises(self):
        key = generate_fernet_key()
        encryptor = APIKeyEncryption(key)
        with pytest.raises(ValueError, match="Cannot encrypt empty value"):
            encryptor.encrypt("")

    def test_decrypt_empty_raises(self):
        key = generate_fernet_key()
        encryptor = APIKeyEncryption(key)
        with pytest.raises(ValueError, match="Cannot decrypt empty value"):
            encryptor.decrypt("")

    def test_decrypt_wrong_key_raises(self):
        key1 = generate_fernet_key()
        key2 = generate_fernet_key()
        encryptor1 = APIKeyEncryption(key1)
        encryptor2 = APIKeyEncryption(key2)
        encrypted = encryptor1.encrypt("secret-data")
        with pytest.raises(ValueError, match="Invalid encrypted value or wrong encryption key"):
            encryptor2.decrypt(encrypted)

    def test_decrypt_tampered_data_raises(self):
        key = generate_fernet_key()
        encryptor = APIKeyEncryption(key)
        encrypted = encryptor.encrypt("secret-data")
        tampered = encrypted[:-5] + "XXXXX"
        with pytest.raises(ValueError, match="Invalid encrypted value or wrong encryption key"):
            encryptor.decrypt(tampered)

    def test_mask_value_delegates_to_masking(self):
        key = generate_fernet_key()
        encryptor = APIKeyEncryption(key)
        with patch("utils.masking.mask_prefix_only") as mock_mask:
            mock_mask.return_value = "sk-proj-***"
            result = encryptor.mask_value("sk-proj-abc123", show_chars=8)
            mock_mask.assert_called_once_with("sk-proj-abc123", 8)
            assert result == "sk-proj-***"


class TestGetEncryptor:
    def test_raises_without_api_key_secret(self, monkeypatch):
        monkeypatch.delenv("API_KEY_SECRET", raising=False)
        # Reset singleton to force re-evaluation
        import utils.encryption as enc_module

        enc_module._encryptor = None
        with pytest.raises(ValueError, match="API_KEY_SECRET environment variable not set"):
            get_encryptor()

    def test_returns_singleton(self, monkeypatch):
        key = generate_fernet_key()
        monkeypatch.setenv("API_KEY_SECRET", key)
        import utils.encryption as enc_module

        enc_module._encryptor = None
        e1 = get_encryptor()
        e2 = get_encryptor()
        assert e1 is e2

    def test_uses_env_secret(self, monkeypatch):
        key = generate_fernet_key()
        monkeypatch.setenv("API_KEY_SECRET", key)
        import utils.encryption as enc_module

        enc_module._encryptor = None
        encryptor = get_encryptor()
        encrypted = encryptor.encrypt("test-secret")
        assert encrypted != "test-secret"


class TestGenerateFernetKey:
    def test_returns_44_char_base64_string(self):
        key = generate_fernet_key()
        assert len(key) == 44
        decoded = base64.urlsafe_b64decode(key.encode())
        assert len(decoded) == 32
