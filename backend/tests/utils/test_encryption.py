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


class TestKeyRing:
    """``API_KEY_SECRET`` encrypts, older keys still decrypt (#2394)."""

    def test_a_value_under_an_old_key_decrypts_with_the_ring(self):
        old, new = generate_fernet_key(), generate_fernet_key()
        token = APIKeyEncryption(old).encrypt("sk-old-secret")
        assert APIKeyEncryption(new, previous_keys=[old]).decrypt(token) == "sk-old-secret"

    def test_a_value_under_an_old_key_is_refused_without_it(self):
        old, new = generate_fernet_key(), generate_fernet_key()
        token = APIKeyEncryption(old).encrypt("sk-old-secret")
        with pytest.raises(ValueError, match="Invalid encrypted value or wrong encryption key"):
            APIKeyEncryption(new).decrypt(token)

    def test_encrypt_uses_the_current_key(self):
        old, new = generate_fernet_key(), generate_fernet_key()
        token = APIKeyEncryption(new, previous_keys=[old]).encrypt("sk-new-secret")
        assert APIKeyEncryption(new).decrypt(token) == "sk-new-secret"
        with pytest.raises(ValueError, match="wrong encryption key"):
            APIKeyEncryption(old).decrypt(token)

    def test_rotate_re_encrypts_under_the_current_key(self):
        old, new = generate_fernet_key(), generate_fernet_key()
        token = APIKeyEncryption(old).encrypt("sk-old-secret")
        ring = APIKeyEncryption(new, previous_keys=[old])
        rotated = ring.rotate(token)
        assert rotated != token
        assert APIKeyEncryption(new).decrypt(rotated) == "sk-old-secret"
        assert ring.is_current(rotated) is True

    def test_is_current_is_false_for_a_token_only_an_old_key_opens(self):
        old, new = generate_fernet_key(), generate_fernet_key()
        token = APIKeyEncryption(old).encrypt("sk-old-secret")
        assert APIKeyEncryption(new, previous_keys=[old]).is_current(token) is False

    def test_is_current_is_true_for_a_token_under_the_current_key(self):
        old, new = generate_fernet_key(), generate_fernet_key()
        ring = APIKeyEncryption(new, previous_keys=[old])
        assert ring.is_current(ring.encrypt("sk-new-secret")) is True

    def test_is_current_raises_when_no_key_opens_the_token(self):
        stranger = APIKeyEncryption(generate_fernet_key()).encrypt("sk-other")
        ring = APIKeyEncryption(generate_fernet_key(), previous_keys=[generate_fernet_key()])
        with pytest.raises(ValueError, match="Invalid encrypted value or wrong encryption key"):
            ring.is_current(stranger)

    def test_rotate_raises_when_no_key_opens_the_token(self):
        stranger = APIKeyEncryption(generate_fernet_key()).encrypt("sk-other")
        ring = APIKeyEncryption(generate_fernet_key(), previous_keys=[generate_fernet_key()])
        with pytest.raises(ValueError, match="Invalid encrypted value or wrong encryption key"):
            ring.rotate(stranger)

    @pytest.mark.parametrize("method", ["rotate", "is_current"])
    def test_empty_token_raises(self, method):
        ring = APIKeyEncryption(generate_fernet_key())
        with pytest.raises(ValueError, match="Cannot decrypt empty value"):
            getattr(ring, method)("")

    @pytest.mark.parametrize(
        "old", [generate_fernet_key(), "a-plain-old-passphrase-not-a-fernet-key"]
    )
    def test_previous_keys_go_through_the_same_derivation(self, old):
        token = APIKeyEncryption(old).encrypt("sk-old-secret")
        ring = APIKeyEncryption("a-plain-new-passphrase", previous_keys=[old])
        assert ring.decrypt(token) == "sk-old-secret"

    def test_empty_duplicate_and_current_entries_are_dropped(self):
        old, new = generate_fernet_key(), generate_fernet_key()
        ring = APIKeyEncryption(new, previous_keys=["", "  ", old, f" {old} ", new])
        assert ring.key_count == 2
        token = APIKeyEncryption(old).encrypt("sk-old-secret")
        assert ring.decrypt(token) == "sk-old-secret"

    def test_a_single_key_ring_has_one_key(self):
        assert APIKeyEncryption(generate_fernet_key()).key_count == 1


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

    def test_reads_the_previous_keys_from_the_settings(self, fernet_api_key_secret, monkeypatch):
        # #2394: a value under API_KEY_SECRET_PREVIOUS stays readable, and a
        # new one is written under API_KEY_SECRET.
        old = generate_fernet_key()
        monkeypatch.setattr(get_settings(), "api_key_secret_previous", f"{old}, ")
        token = APIKeyEncryption(old).encrypt("sk-old-secret")
        encryptor = get_encryptor()
        assert encryptor.key_count == 2
        assert encryptor.decrypt(token) == "sk-old-secret"
        assert encryptor.is_current(token) is False
        fresh = encryptor.encrypt("sk-new-secret")
        assert APIKeyEncryption(fernet_api_key_secret).decrypt(fresh) == "sk-new-secret"

    def test_no_previous_keys_is_a_single_key_ring(self, fernet_api_key_secret, monkeypatch):
        monkeypatch.setattr(get_settings(), "api_key_secret_previous", "")
        assert get_encryptor().key_count == 1

    def test_private_secret_does_not_warn(self, fernet_api_key_secret, caplog):
        with caplog.at_level(logging.WARNING, logger="utils.encryption"):
            get_encryptor()
        assert "development default" not in caplog.text

    @pytest.mark.parametrize(
        "default",
        [Settings.model_fields["api_key_secret"].default, "change-me-api-key-secret"],
    )
    def test_development_default_warns_once(self, monkeypatch, caplog, default):
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
