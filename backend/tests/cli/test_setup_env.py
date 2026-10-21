"""``setup_env`` generates every server secret the production guard checks (#2239).

API_KEY_SECRET, AUDIT_HMAC_KEY and AUDIT_PSEUDO_SALT are generated while they
are missing or a shipped placeholder; a private value is never touched. The
placeholder rule is a stdlib copy of the guard's, kept in step here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cli import setup_env  # noqa: E402
from config import settings as settings_module  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_KEYS = ("API_KEY_SECRET", "AUDIT_HMAC_KEY", "AUDIT_PSEUDO_SALT")


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    """Point setup_env at an empty project root; no prompt, no network."""
    monkeypatch.setattr(setup_env, "_project_root", tmp_path)
    monkeypatch.setattr(setup_env, "_env_example", tmp_path / ".env.example")
    monkeypatch.setattr(setup_env, "_env_local", tmp_path / ".env.local")
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    monkeypatch.setattr("cli._env_probe.probe_self_hosted_available", lambda *a, **k: False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return tmp_path


def _env(project: Path) -> dict[str, str]:
    return setup_env._read_env_file(project / ".env.local")


def test_placeholder_rule_matches_the_production_guard() -> None:
    assert setup_env._PLACEHOLDER_VALUES == settings_module._DEV_SECRET_PLACEHOLDERS
    assert setup_env._PLACEHOLDER_PREFIXES == settings_module._DEV_SECRET_PREFIXES
    assert set(setup_env._GENERATED_SECRETS) == set(
        settings_module._PRODUCTION_SECRET_FIELDS.values()
    )


def test_fresh_install_from_env_example_generates_every_secret(project) -> None:
    example = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    (project / ".env.example").write_text(example, encoding="utf-8")
    shipped = setup_env._read_env_file(project / ".env.example")

    setup_env.setup_env()

    env = _env(project)
    for key in _KEYS:
        # The shipped value is a placeholder, and it is replaced in place.
        assert setup_env._is_placeholder(shipped[key])
        assert not setup_env._is_placeholder(env[key])
        assert len(env[key]) >= 32
        assert (project / ".env.local").read_text(encoding="utf-8").count(f"\n{key}=") == 1
    # Three distinct values.
    assert len({env[key] for key in _KEYS}) == 3


def test_generated_values_pass_the_production_guard(project) -> None:
    setup_env.setup_env()

    env = _env(project)
    values = {field: env[name] for field, name in settings_module._PRODUCTION_SECRET_FIELDS.items()}
    settings = settings_module.Settings(_env_file=None, environment="production", **values)
    assert settings.api_key_secret == env["API_KEY_SECRET"]


def test_private_values_are_kept_and_placeholders_replaced(project, capsys) -> None:
    private = "p" * 40
    (project / ".env.local").write_text(
        f"API_KEY_SECRET={private}\nAUDIT_HMAC_KEY=CHANGE-ME-audit-hmac-key\n"
        "AUDIT_PSEUDO_SALT=kagura-erasure-v1\nOTHER=1\n",
        encoding="utf-8",
    )

    setup_env.setup_env()

    env = _env(project)
    assert env["API_KEY_SECRET"] == private
    assert not setup_env._is_placeholder(env["AUDIT_HMAC_KEY"])
    assert not setup_env._is_placeholder(env["AUDIT_PSEUDO_SALT"])
    assert env["OTHER"] == "1"
    # The original file was backed up first.
    backups = list(project.glob(".env.local.bak.*"))
    assert len(backups) == 1
    assert "kagura-erasure-v1" in backups[0].read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "✓ API_KEY_SECRET already set" in out
    assert "placeholder secret in the existing .env.local was generated" in out


def test_rerun_changes_nothing(project, capsys) -> None:
    setup_env.setup_env()
    first = _env(project)
    capsys.readouterr()

    setup_env.setup_env()

    assert {key: _env(project)[key] for key in _KEYS} == {key: first[key] for key in _KEYS}
    out = capsys.readouterr().out
    for key in _KEYS:
        assert f"✓ {key} already set" in out
    assert "was generated." not in out


def test_a_secret_missing_from_an_existing_file_warns(project, capsys) -> None:
    # An older .env.local without the AUDIT_* lines: the API ran on the defaults.
    (project / ".env.local").write_text(f"API_KEY_SECRET={'p' * 40}\n", encoding="utf-8")

    setup_env.setup_env()

    env = _env(project)
    assert not setup_env._is_placeholder(env["AUDIT_HMAC_KEY"])
    assert "placeholder secret in the existing .env.local was generated" in capsys.readouterr().out


@pytest.mark.parametrize(
    "value",
    [
        "",
        "change-me-api-key-secret",
        "change-me-audit-hmac-key",
        "change-me-audit-pseudo-salt",
        "kagura-erasure-v1",
        "dev-secret-change-in-production",
        "dev-audit-hmac-change-in-production",
        " Change-Me ",
    ],
)
def test_every_shipped_placeholder_is_one(value) -> None:
    assert setup_env._is_placeholder(value)


def test_a_private_value_is_not_a_placeholder() -> None:
    assert not setup_env._is_placeholder("x" * 43)
