"""Discovery and JWKS reuse the parsed signing keys until the key file changes (#2902)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from joserfc.jwk import RSAKey

from api.routes import oidc
from auth import oidc_config
from config.settings import get_settings

ISSUER = "https://identity.example.test/oidc"


def _write(path: Path, keys: list[dict]) -> Path:
    path.write_text(json.dumps({"keys": keys}), encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture(scope="module")
def signing_keys() -> tuple[RSAKey, RSAKey]:
    return RSAKey.generate_key(2048), RSAKey.generate_key(2048)


@pytest.fixture
def ring(tmp_path, signing_keys, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    first, second = signing_keys
    key_file = _write(
        tmp_path / "signing.json",
        [first.as_dict(private=True, kid="current"), second.as_dict(private=True, kid="next")],
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "oidc_enabled", True)
    monkeypatch.setattr(settings, "oidc_issuer", ISSUER)
    monkeypatch.setattr(settings, "oidc_signing_keys_file", str(key_file))
    monkeypatch.setattr(settings, "oidc_active_signing_kid", "current")
    monkeypatch.setattr(oidc_config, "_key_ring_cache", oidc_config._KeyRingCache())
    imported: list[str] = []
    real = RSAKey.import_key.__func__  # type: ignore[attr-defined]

    def counting(cls, value, parameters=None, *args, **kwargs):
        imported.append((parameters or {}).get("kid"))
        return real(cls, value, parameters, *args, **kwargs)

    monkeypatch.setattr(RSAKey, "import_key", classmethod(counting))
    return SimpleNamespace(path=key_file, keys=signing_keys, imported=imported, settings=settings)


@pytest.fixture
def api(ring) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(oidc.router, prefix="/oidc")
    with TestClient(app, base_url="https://identity.example.test") as client:
        yield client


def _kids(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [key["kid"] for key in response.json()["keys"]]


def test_repeated_jwks_and_discovery_requests_import_each_key_once(api, ring):
    for _ in range(10):
        jwks = api.get("/oidc/jwks")
        discovery = api.get("/oidc/.well-known/openid-configuration")
        assert _kids(jwks) == ["current", "next"]
        assert discovery.status_code == 200
        assert discovery.json()["issuer"] == ISSUER
        assert jwks.headers["cache-control"] == "no-store"
        assert discovery.headers["cache-control"] == "no-store"
    assert sorted(ring.imported) == ["current", "next"]


def test_atomically_replaced_key_file_is_served_on_the_next_request(api, ring, tmp_path):
    assert _kids(api.get("/oidc/jwks")) == ["current", "next"]
    first, _ = ring.keys
    staged = _write(tmp_path / "staged.json", [first.as_dict(private=True, kid="current")])
    staged.replace(ring.path)
    assert _kids(api.get("/oidc/jwks")) == ["current"]
    assert _kids(api.get("/oidc/jwks")) == ["current"]
    assert ring.imported.count("current") == 2


def test_invalid_replacement_answers_503(api, ring, tmp_path):
    assert _kids(api.get("/oidc/jwks")) == ["current", "next"]
    _write(tmp_path / "staged.json", [{"kty": "RSA", "kid": "current"}]).replace(ring.path)
    for path in ("/oidc/jwks", "/oidc/.well-known/openid-configuration"):
        response = api.get(path)
        assert response.status_code == 503
        assert response.json() == {"error": "temporarily_unavailable"}


def test_changing_the_active_key_id_setting_takes_effect(api, ring, monkeypatch):
    assert _kids(api.get("/oidc/jwks")) == ["current", "next"]
    before = oidc_config.load_oidc_config()
    monkeypatch.setattr(ring.settings, "oidc_active_signing_kid", "next")
    after = oidc_config.load_oidc_config()
    assert (before.active_kid, after.active_kid) == ("current", "next")
    assert after.private_key.thumbprint() == ring.keys[1].thumbprint()
    assert sorted(ring.imported) == ["current", "current", "next", "next"]
