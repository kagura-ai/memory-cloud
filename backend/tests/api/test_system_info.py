"""Tests for GET /api/v1/system/info feature flags (#1145).

``/system/info`` is a public endpoint; the web UI reads ``features.plan_page``
to decide whether to show the Plan page + sidebar entry. Verify the flag is
exposed and defaults OFF (OSS / self-hosted posture).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import app


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
