"""Operator configuration and provisioning for the isolated OIDC provider."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from joserfc import jwt
from joserfc.jwk import RSAKey

from auth.oidc_config import OIDCConfigurationError, load_oidc_config
from cli.oidc_client import (
    DEFAULT_CLIENT_SCOPE,
    main,
    read_client_secret,
    register_client,
    revoke_client,
    validate_registration,
)


@pytest.fixture(scope="module")
def signing_keys():
    return RSAKey.generate_key(), RSAKey.generate_key()


def _secret_file(path: Path, content: str | bytes) -> Path:
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    path.chmod(0o600)
    return path


def _settings(path: Path, **changes):
    values = {
        "oidc_enabled": True,
        "oidc_issuer": "https://identity.example.test/oidc",
        "oidc_signing_keys_file": str(path),
        "oidc_active_signing_kid": "current",
    }
    return SimpleNamespace(**(values | changes))


def _ring(tmp_path, signing_keys):
    active, previous = signing_keys
    active_jwk = active.as_dict(private=True)
    active_jwk.update(kid="current", alg="RS256", use="sig")
    old_jwk = previous.as_dict()
    old_jwk.update(kid="previous", alg="RS256", use="sig")
    return _secret_file(tmp_path / "keys.json", json.dumps({"keys": [active_jwk, old_jwk]}))


def test_private_active_key_and_previous_public_key_are_loaded(tmp_path, signing_keys):
    config = load_oidc_config(_settings(_ring(tmp_path, signing_keys)))
    assert config.issuer == "https://identity.example.test/oidc"
    assert config.active_kid == "current"
    assert config.private_key.is_private
    assert {key["kid"] for key in config.public_jwks["keys"]} == {"current", "previous"}
    signed = jwt.encode(
        {"alg": "RS256", "kid": config.active_kid},
        {"sub": "person"},
        config.private_key,
        algorithms=["RS256"],
    )
    assert jwt.decode(signed, signing_keys[0], algorithms=["RS256"]).claims["sub"] == "person"
    for public in config.public_jwks["keys"]:
        assert set(public) == {"kty", "n", "e", "kid", "alg", "use"}
        assert not RSAKey.import_key(public).is_private
    assert signing_keys[0].as_dict(private=True)["d"] not in repr(config)


def test_public_jwks_does_not_forward_arbitrary_private_metadata(tmp_path, signing_keys):
    path = _ring(tmp_path, signing_keys)
    data = json.loads(path.read_text())
    data["keys"][0]["operator_secret"] = "must-never-be-published"
    path.write_text(json.dumps(data))
    public = load_oidc_config(_settings(path)).public_jwks
    assert "must-never-be-published" not in json.dumps(public)


def test_pem_private_key_is_accepted(tmp_path, signing_keys):
    path = _secret_file(tmp_path / "key.pem", signing_keys[0].as_pem(private=True))
    config = load_oidc_config(_settings(path))
    assert config.private_key.is_private
    assert [key["kid"] for key in config.public_jwks["keys"]] == ["current"]


def test_disabled_provider_does_not_read_a_key_file(tmp_path):
    with pytest.raises(OIDCConfigurationError, match="disabled"):
        load_oidc_config(_settings(tmp_path / "missing", oidc_enabled=False))


@pytest.mark.parametrize(
    "issuer",
    [
        "http://identity.example.test/oidc",
        "https://identity.example.test/",
        "https://identity.example.test/oidc/",
        "https://identity.example.test/oidc?tenant=one",
        "https://identity.example.test/oidc#fragment",
        "https://person:password@identity.example.test/oidc",
        "https://identity.example.test\\evil/oidc",
        "https://identity.example.test/oidc\n",
    ],
)
def test_issuer_is_a_fixed_https_oidc_url(tmp_path, issuer):
    with pytest.raises(OIDCConfigurationError, match="OIDC_ISSUER"):
        load_oidc_config(_settings(tmp_path / "missing", oidc_issuer=issuer))


@pytest.mark.parametrize("change", ["missing-active", "duplicate", "public-active", "wrong-alg"])
def test_invalid_key_ring_is_refused(tmp_path, signing_keys, change):
    path = _ring(tmp_path, signing_keys)
    data = json.loads(path.read_text())
    settings = _settings(path)
    if change == "missing-active":
        settings.oidc_active_signing_kid = "missing"
    elif change == "duplicate":
        data["keys"][1]["kid"] = "current"
    elif change == "public-active":
        data["keys"][0] = signing_keys[0].as_dict(kid="current")
    else:
        data["keys"][0]["alg"] = "HS256"
    path.write_text(json.dumps(data))
    with pytest.raises(OIDCConfigurationError):
        load_oidc_config(settings)


@pytest.mark.parametrize("kind", ["weak-rsa", "elliptic-curve"])
def test_non_rs256_signing_material_is_refused(tmp_path, kind):
    private = (
        rsa.generate_private_key(public_exponent=65537, key_size=1024)
        if kind == "weak-rsa"
        else ec.generate_private_key(ec.SECP256R1())
    )
    pem = private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    path = _secret_file(tmp_path / "bad.pem", pem)
    with pytest.raises(OIDCConfigurationError):
        load_oidc_config(_settings(path))


def test_configuration_errors_do_not_echo_secret_material(tmp_path, caplog):
    secret = "PRIVATE-SECRET-MATERIAL-NOT-A-KEY"
    path = _secret_file(tmp_path / "invalid", secret)
    with pytest.raises(OIDCConfigurationError) as raised:
        load_oidc_config(_settings(path))
    assert secret not in str(raised.value)
    assert secret not in caplog.text


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits; Windows uses file ACLs")
def test_world_readable_secret_file_is_refused(tmp_path, signing_keys):
    path = _ring(tmp_path, signing_keys)
    path.chmod(0o644)
    with pytest.raises(OIDCConfigurationError, match="permissions"):
        load_oidc_config(_settings(path))


def test_rotation_switches_active_key_while_retaining_previous_jwks(tmp_path, signing_keys):
    path = _ring(tmp_path, signing_keys)
    previous_signature_key = load_oidc_config(_settings(path)).private_key
    active, previous = signing_keys
    replacement = {
        "keys": [active.as_dict(kid="current"), previous.as_dict(private=True, kid="next")]
    }
    staged = _secret_file(tmp_path / "next.json", json.dumps(replacement))
    staged.replace(path)
    config = load_oidc_config(_settings(path, oidc_active_signing_kid="next"))
    assert config.private_key.thumbprint() != previous_signature_key.thumbprint()
    assert {key["kid"] for key in config.public_jwks["keys"]} == {"current", "next"}


def test_caddy_routes_the_oidc_namespace_to_the_api():
    template = Path(__file__).resolve().parents[3] / "terraform/single-server/Caddyfile.tpl"
    text = template.read_text(encoding="utf-8")
    assert "handle /oidc/* {\n\t\treverse_proxy ${API_UPSTREAM}:8080\n\t}" in text
    assert text.index("handle /oidc/*") < text.index("reverse_proxy web:3000")


@pytest.mark.parametrize(
    "redirect",
    [
        "http://client.example.test/callback",
        "https://client.example.test/callback/*",
        "https://client.example.test/callback/%2A",
        "https://person:password@client.example.test/callback",
        "https://client.example.test/callback#fragment",
        "https://client.example.test\\evil/callback",
        "https://client.example.test/callback\n",
    ],
)
def test_operator_rejects_nonexact_or_unsafe_callbacks(redirect):
    with pytest.raises(ValueError):
        validate_registration("web-client", "External web client", [redirect], DEFAULT_CLIENT_SCOPE)


def test_operator_accepts_only_supported_explicit_scopes():
    callbacks = ["https://client.example.test/auth/callback"]
    assert validate_registration(
        "web-client", "External web client", callbacks, "openid profile integrations:manage"
    ) == (callbacks, "openid profile integrations:manage")
    for scope in ("profile", "openid memory:admin", "openid offline_access", "openid unknown"):
        with pytest.raises(ValueError):
            validate_registration("web-client", "External web client", callbacks, scope)


def test_secret_file_preserves_spaces_and_removes_one_line_ending(tmp_path):
    secret = " " + "x" * 40 + " "
    path = _secret_file(tmp_path / "client.secret", secret + "\r\n")
    assert read_client_secret(str(path)) == secret


@pytest.mark.parametrize("secret", ["short", "x" * 73, "x" * 40 + "\nmore"])
def test_invalid_client_secrets_are_refused_without_echo(tmp_path, secret):
    path = _secret_file(tmp_path / "client.secret", secret)
    with pytest.raises(ValueError) as raised:
        read_client_secret(str(path))
    assert secret not in str(raised.value)


def test_noninteractive_registration_requires_a_secret_file(monkeypatch):
    import cli.oidc_client as command

    monkeypatch.setattr(command.sys.stdin, "isatty", lambda: False)
    with pytest.raises(ValueError, match="secret-file"):
        read_client_secret()


@pytest.fixture
def client_database(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    import auth.client_registration
    from models.oidc import OIDCClient

    # This SQLite unit fixture owns secret/hash/output behavior only. The
    # cross-namespace reservation and real advisory-lock races run on PostgreSQL.
    monkeypatch.setattr(
        auth.client_registration, "reserve_oidc_client_id", lambda db, client_id: None
    )

    engine = create_engine("sqlite:///" + str(tmp_path / "clients.sqlite"))
    OIDCClient.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as db:
        yield engine, db
    engine.dispose()


def test_operator_registration_stores_only_bcrypt_and_revocation_is_idempotent(client_database):
    from auth.password import verify_password

    _, db = client_database
    secret = "operator-supplied-" + "x" * 32
    client = register_client(
        db,
        client_id="web-client",
        client_name="External web client",
        redirect_uris=["https://client.example.test/auth/callback"],
        client_secret=secret,
    )
    db.commit()
    assert client.client_secret_hash.startswith("$2")
    assert verify_password(secret, client.client_secret_hash)
    assert client.client_secret_hash != secret
    assert client.scope == DEFAULT_CLIENT_SCOPE
    assert revoke_client(db, "web-client")
    db.commit()
    revoked_at = client.revoked_at
    assert revoked_at is not None
    assert revoke_client(db, "web-client")
    assert client.revoked_at == revoked_at
    assert not client.check_client_secret(secret)
    assert not revoke_client(db, "missing-client")


def test_operator_never_overwrites_an_existing_client(client_database):
    _, db = client_database
    arguments = {
        "client_id": "web-client",
        "client_name": "External web client",
        "redirect_uris": ["https://client.example.test/auth/callback"],
        "client_secret": "client-secret-" + "x" * 32,
    }
    register_client(db, **arguments)
    db.commit()
    with pytest.raises(ValueError, match="already exists"):
        register_client(db, **arguments)


def test_cli_outputs_metadata_without_a_secret_or_hash(
    client_database, tmp_path, monkeypatch, capsys
):
    import cli._env_guard
    import cli.db
    import cli.oidc_client as command

    engine, _ = client_database
    secret = "client-secret-" + "x" * 32
    path = _secret_file(tmp_path / "client.secret", secret)
    monkeypatch.setattr(cli._env_guard, "require_settings", SimpleNamespace)
    monkeypatch.setattr(cli.db, "get_sync_database_url", lambda: "sqlite://")
    monkeypatch.setattr(command, "create_engine", lambda *args, **kwargs: engine)
    result = main(
        [
            "register",
            "--client-id",
            "web-client",
            "--name",
            "External web client",
            "--redirect-uri",
            "https://client.example.test/auth/callback",
            "--secret-file",
            str(path),
        ]
    )
    captured = capsys.readouterr()
    assert result == 0
    assert json.loads(captured.out)["status"] == "registered"
    assert secret not in captured.out + captured.err
    assert "$2" not in captured.out + captured.err


def test_cli_redacts_unicode_input_failures_before_database_access(monkeypatch, capsys):
    import cli._env_guard
    import cli.oidc_client as command

    engine_factory = Mock()
    monkeypatch.setattr(cli._env_guard, "require_settings", SimpleNamespace)
    monkeypatch.setattr(command.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(
        command.getpass, "getpass", Mock(side_effect=["x" * 32, "x" * 31 + "\ud800"])
    )
    monkeypatch.setattr(command, "create_engine", engine_factory)

    result = main(
        [
            "register",
            "--client-id",
            "web-client",
            "--name",
            "External web client",
            "--redirect-uri",
            "https://client.example.test/auth/callback",
        ]
    )
    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "OIDC provisioning could not read its protected inputs\n"
    engine_factory.assert_not_called()


@pytest.mark.skipif(os.name == "nt", reason="Unprivileged symlink creation is POSIX-specific")
def test_private_key_and_client_secret_symlinks_are_refused(tmp_path, signing_keys):
    source = _ring(tmp_path, signing_keys)
    linked = tmp_path / "linked.json"
    linked.symlink_to(source)
    with pytest.raises(OIDCConfigurationError, match="non-symlink"):
        load_oidc_config(_settings(linked))
    with pytest.raises(OIDCConfigurationError, match="non-symlink"):
        read_client_secret(str(linked))
