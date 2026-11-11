"""Tests for GET /api/v1/system/info feature flags (#1145).

``/system/info`` is a public endpoint; the web UI reads ``features.plan_page``
to decide whether to show the Plan page + sidebar entry. Verify the flag is
exposed and defaults OFF (OSS / self-hosted posture).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

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
    "DEFAULT_CONTENT_CHUNKS_ENABLED",
    "CONTENT_CHUNKS_REQUIRE_PLAN",
    "CHATGPT_APP_URL",
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


def test_system_info_exposes_managed_llm_flag_default_off(monkeypatch) -> None:
    """#1569: the analyses UI reads features.managed_llm to stop demanding a
    workspace OpenAI key. Default OFF (no MANAGED_LLM_PROVIDER); ON when set."""
    client = TestClient(app)
    features = client.get("/api/v1/system/info").json()["features"]
    assert features["managed_llm"] is False, "unset MANAGED_LLM_PROVIDER must read OFF"

    monkeypatch.setenv("MANAGED_LLM_PROVIDER", "openai")
    monkeypatch.setenv("MANAGED_LLM_MODEL", "gpt-5-nano")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-platform")
    monkeypatch.setattr("config.settings._settings", None)
    features = client.get("/api/v1/system/info").json()["features"]
    assert features["managed_llm"] is True


def test_system_info_exposes_cost_display_flag_default_on() -> None:
    """#1571: the web UI renders money (cost page + nav, analysis KPI / column /
    estimate) only when features.cost_display is true. Default ON (OSS)."""
    features = TestClient(app).get("/api/v1/system/info").json()["features"]
    assert "cost_display" in features, "cost_display flag must be exposed for the web UI"
    assert features["cost_display"] is True, "ENABLE_COST_DISPLAY must default ON"


def test_system_info_exposes_beta_invites_flag_default_off(info_with) -> None:
    """#1581: the web UI shows the invite card / menu entry / /join page only when
    features.beta_invites is true. Default OFF — the feature grants account
    creation, so it is an explicit opt-in."""
    assert info_with().json()["features"]["beta_invites"] is False
    assert info_with(enable_beta_invites=True).json()["features"]["beta_invites"] is True


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
        "content_chunks_enabled": False,
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
        "content_chunks_enabled": False,
    }
    # Public endpoint: provider/model names only, never the internal URL.
    assert "gpu-rig.internal" not in resp.text


def test_system_info_shows_the_content_chunks_default(info_with) -> None:
    """#2015 D8: what new contexts get from DEFAULT_CONTENT_CHUNKS_ENABLED."""
    body = info_with(default_content_chunks_enabled=True).json()
    assert body["search_defaults"]["content_chunks_enabled"] is True


def test_system_info_exposes_the_content_chunks_plan_gate_default_off(info_with) -> None:
    """#2103: the search settings UI reads whether content chunks are a plan
    feature here. Default OFF: self-hosted deployments keep every plan, no cap."""
    assert info_with().json()["features"]["content_chunks_require_plan"] is False
    gated = info_with(content_chunks_require_plan=True).json()
    assert gated["features"]["content_chunks_require_plan"] is True


def test_system_info_exposes_the_llm_selector_plan_gate_default_off(info_with) -> None:
    """#2144: whether the recall LLM selector is a plan feature, by name only.
    Default OFF: self-hosted deployments keep every plan, no cap."""
    assert info_with().json()["features"]["llm_selector_require_plan"] is False
    gated = info_with(llm_selector_require_plan=True).json()
    assert gated["features"]["llm_selector_require_plan"] is True


# ---------------------------------------------------------------------------
# #2760: chatgpt_app_url (CHATGPT_APP_URL), the deployment's ChatGPT app listing
# ---------------------------------------------------------------------------

_LISTING = "https://example.com/chatgpt-app"


def test_system_info_chatgpt_app_url_is_null_when_unset(info_with) -> None:
    """Unset (the default, self-hosted): null, and nothing else in the document
    moves — the field is the only addition."""
    body = info_with().json()
    assert body["chatgpt_app_url"] is None
    with_url = info_with(chatgpt_app_url=_LISTING).json()
    with_url.pop("chatgpt_app_url")
    body.pop("chatgpt_app_url")
    assert with_url == body


def test_system_info_returns_the_chatgpt_app_url_as_is(info_with) -> None:
    assert info_with(chatgpt_app_url=_LISTING).json()["chatgpt_app_url"] == _LISTING


def test_system_info_chatgpt_app_url_needs_no_auth(info_with) -> None:
    """The endpoint stays public: no cookie, no key, still 200 with the value."""
    resp = info_with(chatgpt_app_url=_LISTING)
    assert resp.json()["chatgpt_app_url"] == _LISTING


def test_chatgpt_app_url_is_trimmed_and_blank_means_unset(monkeypatch) -> None:
    monkeypatch.delenv("CHATGPT_APP_URL", raising=False)
    assert Settings(_env_file=None, chatgpt_app_url=f"  {_LISTING}\n").chatgpt_app_url == _LISTING
    assert Settings(_env_file=None, chatgpt_app_url="   ").chatgpt_app_url == ""
    assert Settings(_env_file=None).chatgpt_app_url == ""
    assert (
        Settings(_env_file=None, chatgpt_app_url="HTTPS://Example.com/x").chatgpt_app_url
        == "HTTPS://Example.com/x"
    )


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com/chatgpt-app",
        "chatgpt-app",
        "javascript:alert(1)",
        "https://",
        "https:///chatgpt-app",
        "//example.com/chatgpt-app",
        # The browser's URL parser refuses these, so the UI would show no link
        # while startup passed: refuse them at startup instead.
        "https://example.com:70000/chatgpt-app",
        "https://example.com:abc/chatgpt-app",
        "https://exa<mple.com/chatgpt-app",
        "https://exa\x07mple.com/chatgpt-app",
        "https://example.com/chatgpt\x7fapp",
        # Userinfo: a link that reads like one host but opens another.
        "https://chatgpt.example@example.com/chatgpt-app",
        "https://user:pass@example.com/chatgpt-app",
    ],
)
def test_chatgpt_app_url_refuses_anything_but_an_https_url_with_a_host(
    value: str, monkeypatch
) -> None:
    """A value the UI could not safely link to fails startup instead."""
    monkeypatch.delenv("CHATGPT_APP_URL", raising=False)
    with pytest.raises(ValidationError, match="CHATGPT_APP_URL"):
        Settings(_env_file=None, chatgpt_app_url=value)
