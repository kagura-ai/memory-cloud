"""``API_KEY_SECRET_PREVIOUS``: older encryption keys kept for decryption (#2394).

``API_KEY_SECRET`` stays the key every new value is encrypted with; the
comma-separated previous keys are only accepted for decryption while
``python -m src.cli.rekey_encrypted_secrets`` moves the stored values over.
Under ``ENVIRONMENT=production`` a placeholder current key is still refused
(#2225); a placeholder among the previous keys only warns, because rotating
away from a weak key is what the list is for. No message carries a key.
"""

import logging
import secrets

import pytest
from pydantic import ValidationError

from config.settings import Settings, split_previous_secrets

_LOGGER = "config.settings"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # A developer shell that sourced .env.local must not decide the outcome.
    for env in (
        "ENVIRONMENT",
        "API_KEY_SECRET",
        "API_KEY_SECRET_PREVIOUS",
        "AUDIT_HMAC_KEY",
        "AUDIT_PSEUDO_SALT",
    ):
        monkeypatch.delenv(env, raising=False)


def _private() -> dict[str, str]:
    return {
        "api_key_secret": secrets.token_hex(32),
        "audit_hmac_key": secrets.token_hex(32),
        "audit_pseudo_salt": secrets.token_hex(16),
    }


class TestSplitPreviousSecrets:
    def test_splits_on_commas_and_strips(self):
        assert split_previous_secrets(" a-old-key , b-old-key ", current="c") == (
            "a-old-key",
            "b-old-key",
        )

    def test_drops_empty_entries(self):
        assert split_previous_secrets(",, a-old-key,  ,", current="c") == ("a-old-key",)

    def test_drops_duplicates_keeping_the_first_position(self):
        assert split_previous_secrets("b,a,b , a", current="c") == ("b", "a")

    def test_drops_an_entry_equal_to_the_current_key(self):
        assert split_previous_secrets("a, current-key ,b", current="current-key") == ("a", "b")

    @pytest.mark.parametrize("raw", ["", "   ", ",", " , , "])
    def test_nothing_set_is_an_empty_tuple(self, raw):
        assert split_previous_secrets(raw, current="c") == ()


class TestSettingsField:
    def test_unset_means_no_previous_keys(self):
        settings = Settings(_env_file=None)
        assert settings.api_key_secret_previous == ""
        assert settings.api_key_secret_previous_keys == ()

    def test_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("API_KEY_SECRET", "current-key-for-the-test")
        monkeypatch.setenv("API_KEY_SECRET_PREVIOUS", "old-key-one, old-key-two")
        settings = Settings(_env_file=None)
        assert settings.api_key_secret_previous_keys == ("old-key-one", "old-key-two")

    def test_an_entry_equal_to_the_current_key_is_dropped_with_a_warning(self, caplog):
        current = secrets.token_hex(32)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            settings = Settings(
                _env_file=None,
                api_key_secret=current,
                api_key_secret_previous=f"old-key, {current}",
            )
        assert settings.api_key_secret_previous_keys == ("old-key",)
        warnings = [r for r in caplog.records if "API_KEY_SECRET_PREVIOUS" in r.getMessage()]
        assert len(warnings) == 1
        assert warnings[0].levelno == logging.WARNING
        assert current not in caplog.text
        assert "old-key" not in caplog.text

    def test_distinct_previous_keys_do_not_warn_outside_production(self, caplog):
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            Settings(_env_file=None, api_key_secret_previous="change-me-api-key-secret")
        assert "API_KEY_SECRET_PREVIOUS" not in caplog.text

    def test_an_exported_empty_list_wins_over_the_env_file(self, monkeypatch, tmp_path):
        # The re-key CLI exports "" when the API container has no list, so a
        # dotenv list never pairs with the container's key.
        env_file = tmp_path / ".env.test"
        env_file.write_text("API_KEY_SECRET_PREVIOUS=old-key-from-a-dotenv-file\n")
        assert Settings(_env_file=env_file).api_key_secret_previous_keys == (
            "old-key-from-a-dotenv-file",
        )
        monkeypatch.setenv("API_KEY_SECRET_PREVIOUS", "")
        assert Settings(_env_file=env_file).api_key_secret_previous_keys == ()


class TestCurrentKeyThatCannotBeListedVerbatim:
    """A current key with a comma or blanks needs its derived Fernet key later."""

    _MESSAGE = "--derive-fernet-key"

    @pytest.mark.parametrize(
        ("current", "environment"),
        [
            (" s3cret-with-blanks-around-it-for-the-guard-test ", "development"),
            ("s3cret-with,a-comma-in-it-for-the-guard-test-2394", "development"),
            (f"\t{secrets.token_hex(32)}", "production"),
        ],
        ids=["blanks", "comma", "production"],
    )
    def test_warns_once_without_the_value(self, caplog, current, environment):
        values = _private() | {"api_key_secret": current}
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            Settings(_env_file=None, environment=environment, **values)
        warnings = [r for r in caplog.records if self._MESSAGE in r.getMessage()]
        assert len(warnings) == 1
        assert warnings[0].levelno == logging.WARNING
        for part in current.split(","):
            assert part.strip() not in caplog.text

    def test_a_clean_key_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            Settings(_env_file=None, environment="production", **_private())
        assert self._MESSAGE not in caplog.text


class TestProductionGuard:
    @pytest.mark.parametrize(
        "weak",
        [
            "dev-secret-change-in-production",
            "change-me-api-key-secret",
            "short-private-key",
        ],
    )
    def test_a_weak_previous_key_warns_and_does_not_refuse(self, caplog, weak):
        values = _private() | {"api_key_secret_previous": f"{secrets.token_hex(32)},{weak}"}
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            settings = Settings(_env_file=None, environment="production", **values)
        assert len(settings.api_key_secret_previous_keys) == 2
        warnings = [r for r in caplog.records if "API_KEY_SECRET_PREVIOUS" in r.getMessage()]
        assert len(warnings) == 1
        assert warnings[0].levelno == logging.WARNING
        assert "rekey_encrypted_secrets" in warnings[0].getMessage()
        assert weak not in caplog.text

    def test_private_previous_keys_do_not_warn(self, caplog):
        values = _private() | {"api_key_secret_previous": secrets.token_hex(32)}
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            Settings(_env_file=None, environment="production", **values)
        assert "API_KEY_SECRET_PREVIOUS" not in caplog.text

    @pytest.mark.parametrize("placeholder", ["dev-secret-change-in-production", "x" * 31])
    def test_the_current_key_is_still_refused(self, placeholder):
        values = _private() | {
            "api_key_secret": placeholder,
            "api_key_secret_previous": secrets.token_hex(32),
        }
        with pytest.raises(ValidationError, match="API_KEY_SECRET") as exc:
            Settings(_env_file=None, environment="production", **values)
        assert placeholder not in str(exc.value)
        # The previous list is not a refused variable.
        assert "API_KEY_SECRET_PREVIOUS" not in str(exc.value)
