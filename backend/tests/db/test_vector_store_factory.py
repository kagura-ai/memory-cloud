"""Tests for the VectorStore backend selector (db/vector_store.py).

Pure-Python — does NOT require lancedb to be installed. Constructing a
LanceVectorStore does not import lancedb (that happens lazily on first use),
so the factory routing can be verified without the optional dependency.
"""

import types

import pytest

from db.vector_store import get_active_store, reset_vector_store


@pytest.fixture(autouse=True)
def _reset_backend():
    reset_vector_store()
    yield
    reset_vector_store()


def _fake_settings(backend: str, path: str = "./data/test.lance"):
    return types.SimpleNamespace(vector_backend=backend, lance_db_path=path)


def test_default_backend_is_qdrant(monkeypatch):
    monkeypatch.setattr("config.settings.get_settings", lambda: _fake_settings("qdrant"))
    assert get_active_store() is None


def test_empty_backend_resolves_to_qdrant(monkeypatch):
    monkeypatch.setattr("config.settings.get_settings", lambda: _fake_settings(""))
    assert get_active_store() is None


def test_backend_is_case_insensitive(monkeypatch):
    monkeypatch.setattr("config.settings.get_settings", lambda: _fake_settings("QDRANT"))
    assert get_active_store() is None


def test_lance_backend_returns_lance_store(monkeypatch):
    monkeypatch.setattr("config.settings.get_settings", lambda: _fake_settings("lance"))
    from db.lance_store import LanceVectorStore

    store = get_active_store()
    assert isinstance(store, LanceVectorStore)


def test_resolution_is_cached(monkeypatch):
    calls = {"n": 0}

    def _gs():
        calls["n"] += 1
        return _fake_settings("qdrant")

    monkeypatch.setattr("config.settings.get_settings", _gs)
    assert get_active_store() is None
    assert get_active_store() is None
    assert calls["n"] == 1  # settings read once, then cached


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setattr("config.settings.get_settings", lambda: _fake_settings("weaviate"))
    with pytest.raises(ValueError, match="Unknown vector_backend"):
        get_active_store()


def test_reset_allows_rebinding(monkeypatch):
    monkeypatch.setattr("config.settings.get_settings", lambda: _fake_settings("qdrant"))
    assert get_active_store() is None
    monkeypatch.setattr("config.settings.get_settings", lambda: _fake_settings("lance"))
    assert get_active_store() is None  # still cached as qdrant
    reset_vector_store()
    from db.lance_store import LanceVectorStore

    assert isinstance(get_active_store(), LanceVectorStore)
