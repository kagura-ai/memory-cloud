"""Tests for utils/encryption.py.

Pure utility — no DB or external services required.
"""

import base64
import logging
from unittest.mock import patch

import pytest

import utils.encryption as enc_module
from config.settings import Settings, get_settings
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
    """``get_encryptor`` reads ``API_KEY_SECRET`` from the cached settings (#2239)."""

    @pytest.mark.parametrize("value", ["", "   "])
    def test_refuses_an_empty_api_key_secret(self, monkeypatch, value):
        monkeypatch.setattr(get_settings(), "api_key_secret", value)
        monkeypatch.setattr(enc_module, "_encryptor", None)
        with pytest.raises(ValueError, match="API_KEY_SECRET is empty"):
            get_encryptor()

    def test_returns_singleton(self, fernet_api_key_secret):
        e1 = get_encryptor()
        e2 = get_encryptor()
        assert e1 is e2

    def test_uses_the_settings_secret(self, fernet_api_key_secret):
        encrypted = get_encryptor().encrypt("test-secret")
        assert encrypted != "test-secret"
        assert APIKeyEncryption(fernet_api_key_secret).decrypt(encrypted) == "test-secret"

    def test_ignores_a_process_env_set_after_the_settings(self, fernet_api_key_secret, monkeypatch):
        monkeypatch.setenv("API_KEY_SECRET", generate_fernet_key())
        encrypted = get_encryptor().encrypt("test-secret")
        assert APIKeyEncryption(fernet_api_key_secret).decrypt(encrypted) == "test-secret"

    def test_private_secret_does_not_warn(self, fernet_api_key_secret, caplog):
        with caplog.at_level(logging.WARNING, logger="utils.encryption"):
            get_encryptor()
        assert "development default" not in caplog.text

    def test_development_default_warns_once(self, monkeypatch, caplog):
        default = Settings.model_fields["api_key_secret"].default
        monkeypatch.setattr(get_settings(), "api_key_secret", default)
        monkeypatch.setattr(enc_module, "_encryptor", None)
        with caplog.at_level(logging.WARNING, logger="utils.encryption"):
            get_encryptor()
            get_encryptor()
        warnings = [r for r in caplog.records if "development default" in r.getMessage()]
        assert len(warnings) == 1
        assert warnings[0].levelno == logging.WARNING
        assert default not in caplog.text


class TestGenerateFernetKey:
    def test_returns_44_char_base64_string(self):
        key = generate_fernet_key()
        assert len(key) == 44
        decoded = base64.urlsafe_b64decode(key.encode())
        assert len(decoded) == 32
