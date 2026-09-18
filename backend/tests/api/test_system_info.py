"""Tests for GET /api/v1/system/info feature flags (#1145).

``/system/info`` is a public endpoint; the web UI reads ``features.plan_page``
to decide whether to show the Plan page + sidebar entry. Verify the flag is
exposed and defaults OFF (OSS / self-hosted posture).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app
from config.settings import Settings

_RERANK_ENV = [
    "ENABLE_RERANKING",
    "RERANK_BASE_URL",
    "RERANK_MODEL",
    "SELF_HOSTED_BASE_URL",
    "DEFAULT_RERANKER_PROVIDER",
    "DEFAULT_USE_RERANK",
    "DEFAULT_RERANKER_MODEL",
]


@pytest.fixture
def info_with(monkeypatch):
    """GET /system/info under a fresh Settings built from ``overrides`` only
    (env keys that would leak into the fields are removed first)."""
    for key in _RERANK_ENV:
        monkeypatch.delenv(key, raising=False)

    def _get(**overrides):
        settings = Settings(_env_file=None, **overrides)
        monkeypatch.setattr("config.settings.get_settings", lambda: settings)
        resp = TestClient(app).get("/api/v1/system/info")
        assert resp.status_code == 200
        return resp

    return _get


def test_system_info_exposes_plan_page_flag_default_off() -> None:
    client = TestClient(app)
    resp = client.get("/api/v1/system/info")
    assert resp.status_code == 200

    features = resp.json()["features"]
    assert "plan_page" in features, "plan_page flag must be exposed for the web UI"
    assert features["plan_page"] is False, "ENABLE_PLAN_PAGE must default OFF"


def test_system_info_exposes_managed_connectors_flag_default_off() -> None:
    """#1426: the connectors UI reads features.managed_connectors to hide the BYO
    form + drop the per-connector LLM requirement. Default OFF (OSS/self-host)."""
    client = TestClient(app)
    resp = client.get("/api/v1/system/info")
    assert resp.status_code == 200

    features = resp.json()["features"]
    assert "managed_connectors" in features, (
        "managed_connectors flag must be exposed for the web UI"
    )
    assert features["managed_connectors"] is False, "ENABLE_MANAGED_CONNECTORS must default OFF"


def test_system_info_exposes_cost_display_flag_default_on() -> None:
    """#1571: the web UI renders money (cost page + nav, analysis KPI / column /
    estimate) only when features.cost_display is true. Default ON (OSS)."""
    features = TestClient(app).get("/api/v1/system/info").json()["features"]
    assert "cost_display" in features, "cost_display flag must be exposed for the web UI"
    assert features["cost_display"] is True, "ENABLE_COST_DISPLAY must default ON"


# ---------------------------------------------------------------------------
# #1572: features.reranking + search_defaults
# ---------------------------------------------------------------------------


def test_system_info_reranking_on_and_todays_defaults_when_unset(info_with) -> None:
    """ENABLE_RERANKING defaults ON and the voyage default may have a BYOK key
    per workspace, so the feature reads available; search_defaults is the
    deployment default new contexts get (today's values when unset)."""
    body = info_with().json()
    assert body["features"]["reranking"] is True
    assert body["search_defaults"] == {
        "use_rerank": False,
        "reranker_provider": "voyage",
        "reranker_model": "rerank-2",
    }


def test_system_info_reranking_false_when_disabled_by_deployment(info_with) -> None:
    body = info_with(enable_reranking=False).json()
    assert body["features"]["reranking"] is False


def test_system_info_self_hosted_default_without_endpoint_is_not_available(info_with) -> None:
    """A self_hosted default that is OFF boots fine but has nowhere to rerank —
    the web UI must not present reranking as available."""
    body = info_with(default_reranker_provider="self_hosted").json()
    assert body["features"]["reranking"] is False
    assert body["search_defaults"]["reranker_provider"] == "self_hosted"


def test_system_info_self_hosted_default_with_endpoint_exposes_model_not_url(info_with) -> None:
    resp = info_with(
        rerank_base_url="http://gpu-rig.internal:8002",
        rerank_model="qwen3-reranker-4b",
        default_reranker_provider="self_hosted",
        default_use_rerank=True,
    )
    body = resp.json()
    assert body["features"]["reranking"] is True
    assert body["search_defaults"] == {
        "use_rerank": True,
        "reranker_provider": "self_hosted",
        "reranker_model": "qwen3-reranker-4b",
    }
    # Public endpoint: provider/model names only, never the internal URL.
    assert "gpu-rig.internal" not in resp.text
