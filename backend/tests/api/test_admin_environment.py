"""Admin read of the effective environment settings (#2837).

``GET /api/v1/admin/environment`` in every ``SYSTEM_ADMIN_MODE``. The route
needs no database (``get_db`` fails the test if it is opened) and makes no
outbound call (the embedding probe and the async HTTP client fail the test).
The values under test are set on the process's ``Settings`` object, so every
assertion is about the process that answers.
"""

from __future__ import annotations

import secrets
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from api.main import app
from api.routes import config as config_routes
from api.routes import system as system_routes
from auth.dependencies import get_current_user_optional
from config.constants import EMBEDDING_MODEL_REGISTRY
from config.settings import Settings, get_settings
from db.base import get_db

URL = "/api/v1/admin/environment"
ADMIN_SESSION = {"user_id": "1001", "email": "admin@example.com", "role": "admin"}
USER_SESSION = {"user_id": "1002", "email": "user@example.com", "role": "user"}
OPERATOR = "ops@example.com"
MARKER = "***MASKED***"
ITEM_KEYS = {"key", "category", "value", "type", "description", "requires_restart"}
SCHEMA_ONLY_FIELDS = (
    "default_value",
    "enum_values",
    "enum_descriptions",
    "examples",
    "recommended",
    "impact",
    "documentation_url",
    "is_sensitive",
    "min_value",
    "max_value",
)


def _token(length: int = 40) -> str:
    return ("tok" + secrets.token_hex(length))[:length]


@pytest.fixture
def mode(monkeypatch: pytest.MonkeyPatch):
    def _apply(value: str, **values: str) -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "system_admin_mode", value)
        defaults = {
            "system_admin_service_token": "",
            "system_admin_service_token_previous": "",
            "system_admin_service_allowed_cidrs": "",
        }
        defaults.update(values)
        for field, item in defaults.items():
            monkeypatch.setattr(settings, field, item)

    return _apply


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    async def _no_db():
        raise AssertionError("the environment read must not open a database session")
        yield  # pragma: no cover

    async def _no_probe(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the environment read must not probe an embedding endpoint")

    async def _no_send(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the environment read must not make an outbound call")

    monkeypatch.setattr(system_routes, "_fetch_v1_models", _no_probe)
    monkeypatch.setattr(httpx.AsyncClient, "send", _no_send)
    app.dependency_overrides[get_db] = _no_db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _as(principal: dict | None) -> None:
    if principal is None:
        app.dependency_overrides.pop(get_current_user_optional, None)
        return

    async def _session():
        return dict(principal)

    app.dependency_overrides[get_current_user_optional] = _session


@pytest.fixture
def admin(client, mode):
    mode("session")
    _as(ADMIN_SESSION)
    return client


def _items(response) -> dict[str, dict]:
    assert response.status_code == 200, response.text
    return {item["key"]: item for item in response.json()["configs"]}


def _key_count() -> int:
    """How many keys the categories hold today (derived, not pinned)."""
    return sum(len(keys) for keys in config_routes.get_config_categories().values())


def _with_category_key(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    """Put ``key`` at the end of the ``hosted`` category, as a later change would."""
    categories = config_routes.get_config_categories()
    categories["hosted"].append(key)
    monkeypatch.setattr(config_routes, "get_config_categories", lambda: categories)


# --- the allow-list ------------------------------------------------------------------


def test_the_answered_keys_are_exactly_the_category_keys() -> None:
    from api.routes.admin_environment import ANSWERED_KEYS, REDACTED_VALUE

    category_keys = [key for keys in config_routes.get_config_categories().values() for key in keys]
    assert isinstance(ANSWERED_KEYS, frozenset)
    # D3: a key added to the categories is withheld until it is added here on purpose
    assert ANSWERED_KEYS == set(category_keys)
    assert len(category_keys) == len(ANSWERED_KEYS) == _key_count()
    assert ANSWERED_KEYS & config_routes.get_sensitive_keys() == set()
    assert REDACTED_VALUE == MARKER


# --- the gate ------------------------------------------------------------------------


def test_session_admin_gets_every_category_key_in_order_with_its_effective_value(
    client, mode, monkeypatch
) -> None:
    mode("session")
    _as(ADMIN_SESSION)
    settings = get_settings()
    monkeypatch.setattr(settings, "managed_llm_provider", "self_hosted")
    monkeypatch.setattr(settings, "managed_llm_model", "model-x")
    monkeypatch.setattr(settings, "enable_plan_page", True)
    monkeypatch.setattr(settings, "rerank_base_url", "https://rerank.example.com/v1")
    monkeypatch.setattr(settings, "cors_origins", "https://a.example.com,https://b.example.com")
    monkeypatch.setenv("ENABLE_DECAY", "false")

    response = client.get(URL)
    assert response.status_code == 200, response.text
    body = response.json()
    # D8: the two top-level keys, nothing else
    assert set(body) == {"configs", "embedding_models"}
    categories = config_routes.get_config_categories()
    schema = config_routes.get_config_schema()
    expected = [
        {
            "key": key,
            "category": category,
            "value": config_routes.get_effective_value(key, settings),
            "type": schema[key].type,
            "description": schema[key].description,
            "requires_restart": schema[key].requires_restart,
        }
        for category, keys in categories.items()
        for key in keys
    ]
    assert body["configs"] == expected
    assert [item["category"] for item in body["configs"]].count("hosted") == 12
    for item in body["configs"]:
        assert set(item) == ITEM_KEYS
        assert item["requires_restart"] is True
    values = {item["key"]: item["value"] for item in body["configs"]}
    assert values["MANAGED_LLM_PROVIDER"] == "self_hosted"
    assert values["MANAGED_LLM_MODEL"] == "model-x"
    assert values["ENABLE_PLAN_PAGE"] is True
    assert values["RERANK_BASE_URL"] == "https://rerank.example.com/v1"
    assert values["CORS_ORIGINS"] == "https://a.example.com,https://b.example.com"
    assert values["ENABLE_DECAY"] is False
    assert isinstance(values["EMBEDDING_DIMENSIONS"], int)
    assert MARKER not in response.text


def test_session_refusals(client, mode) -> None:
    mode("session")
    _as(None)
    assert client.get(URL).status_code == 401
    _as(USER_SESSION)
    refused = client.get(URL)
    assert refused.status_code == 403
    assert "configs" not in refused.text


def test_off_is_an_unknown_path(client, mode) -> None:
    mode("off")
    _as(ADMIN_SESSION)
    response = client.get(URL)
    assert response.status_code == 404
    assert response.json() == {"error": "HTTP-404", "message": "Not Found", "details": {}}


def test_service_operator_gets_the_hosted_settings_too(client, mode, monkeypatch) -> None:
    token = secrets.token_hex(32)
    mode("service", system_admin_service_token=token)
    _as(None)
    monkeypatch.setattr(get_settings(), "managed_llm_model", "model-x")
    headers = {"Authorization": f"Bearer {token}", "X-Operator-Email": OPERATOR}
    with capture_logs() as logs:
        response = client.get(URL, headers=headers)
    items = _items(response)
    hosted = [key for key, item in items.items() if item["category"] == "hosted"]
    assert hosted == config_routes.get_config_categories()["hosted"]
    assert items["MANAGED_LLM_MODEL"]["value"] == "model-x"
    assert len(items) == _key_count()
    [entry] = [e for e in logs if e["event"] == "admin_environment_read"]
    assert entry == {
        "event": "admin_environment_read",
        "log_level": "info",
        "admin_user": f"operator:{OPERATOR}",
        "count": _key_count(),
    }
    # the same answer as a session admin gets
    operator_body = response.json()
    _as(ADMIN_SESSION)
    refused = client.get(URL)
    assert refused.status_code == 401
    assert refused.json()["error"] == "SYSADMIN-002"
    mode("session")
    assert client.get(URL).json() == operator_body


# --- a withheld key answers the marker, whatever it holds ---------------------------


def _assert_withheld(response, key: str, sentinel: str) -> None:
    items = _items(response)
    assert items[key]["value"] == MARKER
    assert sentinel not in response.text
    # no part of the value: not its first or last four characters either
    assert sentinel[:4] not in response.text
    assert sentinel[-4:] not in response.text


def _edge_sentinel() -> str:
    """40 characters whose first and last four occur nowhere else in an answer."""
    return "Qz7~" + secrets.token_hex(16) + "~9Xw"


def test_a_sensitive_key_added_to_the_categories_is_withheld(admin, monkeypatch) -> None:
    # N1
    sentinel = _edge_sentinel()
    assert len(sentinel) == 40
    assert "GOOGLE_CLIENT_SECRET" in config_routes.get_sensitive_keys()
    before = _key_count()
    _with_category_key(monkeypatch, "GOOGLE_CLIENT_SECRET")
    monkeypatch.setattr(get_settings(), "google_client_secret", sentinel)
    response = admin.get(URL)
    _assert_withheld(response, "GOOGLE_CLIENT_SECRET", sentinel)
    assert len(_items(response)) == before + 1


def test_an_answered_key_that_becomes_sensitive_is_withheld(admin, monkeypatch) -> None:
    # N2: the marker, not the partial mask of ``mask_sensitive_value``
    sentinel = _edge_sentinel()
    sensitive = config_routes.get_sensitive_keys() | {"MANAGED_LLM_MODEL"}
    monkeypatch.setattr(config_routes, "get_sensitive_keys", lambda: sensitive)
    monkeypatch.setattr(get_settings(), "managed_llm_model", sentinel)
    assert config_routes.mask_sensitive_value("MANAGED_LLM_MODEL", sentinel).startswith("Qz7~")
    response = admin.get(URL)
    _assert_withheld(response, "MANAGED_LLM_MODEL", sentinel)
    assert "*" * 8 not in response.text.replace(MARKER, "")


def test_a_category_key_on_neither_list_is_withheld(admin, monkeypatch) -> None:
    # N3: answered keys are an allow-list, not "every key that is not sensitive"
    sentinel = _edge_sentinel()
    assert "audit_hmac_key" in Settings.model_fields
    assert "AUDIT_HMAC_KEY" not in config_routes.get_sensitive_keys()
    _with_category_key(monkeypatch, "AUDIT_HMAC_KEY")
    monkeypatch.setattr(get_settings(), "audit_hmac_key", sentinel)
    response = admin.get(URL)
    _assert_withheld(response, "AUDIT_HMAC_KEY", sentinel)
    item = _items(response)["AUDIT_HMAC_KEY"]
    assert item["category"] == "hosted"
    # a key without a schema entry
    assert (item["type"], item["description"], item["requires_restart"]) == (None, None, None)


@pytest.mark.parametrize("value", ["", "short", "x" * 9, None, 0, False, 12345678901234])
def test_the_marker_does_not_depend_on_the_value(admin, monkeypatch, value) -> None:
    _with_category_key(monkeypatch, "AUDIT_HMAC_KEY")
    monkeypatch.setattr(get_settings(), "audit_hmac_key", value)
    assert _items(admin.get(URL))["AUDIT_HMAC_KEY"]["value"] == MARKER


def test_a_withheld_key_is_never_resolved(admin, monkeypatch) -> None:
    answered = _key_count()
    _with_category_key(monkeypatch, "AUDIT_HMAC_KEY")
    resolved: list[str] = []
    original = config_routes.get_effective_value

    def _recording(key: str, settings: Settings) -> Any:
        resolved.append(key)
        return original(key, settings)

    monkeypatch.setattr(config_routes, "get_effective_value", _recording)
    assert admin.get(URL).status_code == 200
    assert len(resolved) == answered
    assert "AUDIT_HMAC_KEY" not in resolved


# --- URL credentials ---------------------------------------------------------------


def test_url_credentials_are_masked_in_any_string_value(admin, monkeypatch) -> None:
    # N4
    sentinel = _token()
    url = f"https://user:{sentinel}@example.com/v1"
    settings = get_settings()
    monkeypatch.setattr(settings, "rerank_base_url", url)
    monkeypatch.setattr(settings, "cors_origins", url)
    monkeypatch.setattr(settings, "managed_llm_model", url)
    response = admin.get(URL)
    items = _items(response)
    assert items["RERANK_BASE_URL"]["value"] == "https://***@example.com/v1"
    # not only the keys of ``get_url_keys()``
    assert config_routes.get_url_keys() == {"RERANK_BASE_URL"}
    assert items["CORS_ORIGINS"]["value"] == "https://***@example.com/v1"
    assert items["MANAGED_LLM_MODEL"]["value"] == "https://***@example.com/v1"
    assert sentinel not in response.text


# --- embedding models --------------------------------------------------------------


def test_embedding_models_lists_what_the_deployment_offers(admin, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "embedding_model", "text-embedding-3-large")
    monkeypatch.setattr(settings, "embedding_model_allowlist", "")
    response = admin.get(URL)
    assert response.status_code == 200, response.text
    models = response.json()["embedding_models"]
    assert set(models) == {"default_model", "offered"}
    assert models["default_model"] == "text-embedding-3-large"
    assert models["offered"] == [
        {"name": name, "dimensions": dimensions, "provider": provider}
        for name, (dimensions, provider) in EMBEDDING_MODEL_REGISTRY.items()
    ]
    assert len(models["offered"]) >= 2
    for model in models["offered"]:
        assert set(model) == {"name", "dimensions", "provider"}


def test_embedding_models_follow_the_allowlist(admin, monkeypatch) -> None:
    from config.embedding_policy import allowed_embedding_models

    settings = get_settings()
    first, second = list(EMBEDDING_MODEL_REGISTRY)[:2]
    monkeypatch.setattr(settings, "embedding_model", first)
    monkeypatch.setattr(settings, "embedding_model_allowlist", first)
    assert set(allowed_embedding_models(first)) == {first}
    models = admin.get(URL).json()["embedding_models"]
    assert models == {
        "default_model": first,
        "offered": [
            {
                "name": first,
                "dimensions": EMBEDDING_MODEL_REGISTRY[first][0],
                "provider": EMBEDDING_MODEL_REGISTRY[first][1],
            }
        ],
    }
    assert second not in str(models)


def test_no_stored_key_environment_key_or_endpoint_is_read(admin, monkeypatch) -> None:
    # N5: the client fixture fails the test on get_db, the probe and any outbound call
    key, self_hosted_key, host = _token(), _token(), _token(20)
    settings = get_settings()
    monkeypatch.setenv("OPENAI_API_KEY", key)
    monkeypatch.setattr(settings, "embedding_provider", "self_hosted")
    monkeypatch.setattr(settings, "self_hosted_api_key", self_hosted_key)
    monkeypatch.setattr(settings, "self_hosted_base_url", f"http://{host}.example.com:8000/v1")
    response = admin.get(URL)
    assert response.status_code == 200, response.text
    for sentinel in (key, self_hosted_key, host):
        assert sentinel not in response.text
    assert "available" not in response.text
    assert _items(response)["EMBEDDING_PROVIDER"]["value"] == "self_hosted"


# --- shape -------------------------------------------------------------------------


def test_a_query_parameter_changes_nothing(admin, monkeypatch) -> None:
    # N6
    sentinel = _edge_sentinel()
    _with_category_key(monkeypatch, "GOOGLE_CLIENT_SECRET")
    monkeypatch.setattr(get_settings(), "google_client_secret", sentinel)
    plain = admin.get(URL)
    for query in ("mask_sensitive=false", "mask_sensitive=true", "category=hosted", "limit=1"):
        asked = admin.get(f"{URL}?{query}")
        assert asked.status_code == 200, query
        assert asked.json() == plain.json(), query
        assert sentinel not in asked.text
    operation = app.openapi()["paths"][URL]["get"]
    assert operation.get("parameters", []) == []


def test_no_schema_only_field_and_no_telemetry_is_answered(admin) -> None:
    response = admin.get(URL)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"configs", "embedding_models"}
    for name in SCHEMA_ONLY_FIELDS:
        assert f'"{name}"' not in response.text
    # the schema also describes keys outside the categories: none is answered
    answered = {item["key"] for item in body["configs"]}
    outside = set(config_routes.get_config_schema()) - answered
    assert outside
    for key in outside:
        assert f'"{key}"' not in response.text
    for name in ("qdrant", "postgres", "redis", "collection", "version", "hostname", "health"):
        assert f'"{name}"' not in response.text.lower()


def test_the_log_line_names_the_caller_and_the_count_only(admin, monkeypatch) -> None:
    sentinel = _token()
    monkeypatch.setattr(get_settings(), "managed_llm_model", sentinel)
    monkeypatch.setattr(get_settings(), "rerank_base_url", f"https://rerank-{sentinel}.example.com")
    with capture_logs() as logs:
        assert admin.get(URL).status_code == 200
    [entry] = [e for e in logs if e["event"] == "admin_environment_read"]
    assert entry == {
        "event": "admin_environment_read",
        "log_level": "info",
        "admin_user": "1001",
        "count": _key_count(),
    }
    assert sentinel not in repr(logs)


# --- sign-up, rewards and operations (#2954) -----------------------------------------

NEW_CATEGORY_VALUES: dict[str, dict[str, Any]] = {
    "signup": {
        "ALLOW_REGISTRATION": True,
        "ENABLE_EMAIL_SIGNUP": True,
        "ENABLE_PROVIDER_LINK_CONFIRMATION": False,
        "ENABLE_BETA_INVITES": True,
        "BETA_INVITE_QUOTA_PER_USER": 7,
    },
    "rewards": {
        "ENABLE_REFERRALS": True,
        "REFERRAL_REFEREE_REWARD_MEMORIES": 123,
        "REFERRAL_REFERRER_REWARD_MEMORIES": 456,
        "REFERRAL_MAX_GRANTS_PER_REFERRER": 9,
        "REFERRAL_REDEEM_WINDOW_HOURS": 48,
    },
    "operations": {
        "MCP_NOTICES_ENABLED": True,
        "REQUIRE_DUAL_CONTROL_FORCE_TRANSFER": True,
        "BM25_DRIFT_CRON_ENABLED": True,
        "BM25_REVEAL_RATE_LIMIT_PER_HOUR": 17,
        "OPERATIONS_PROBE_INTERVAL_SECONDS": 600,
        "FILE_SCAN_ENABLED": True,
        "SLEEP_LLM_FORCE_ENV": True,
    },
}


def _set_effective(monkeypatch: pytest.MonkeyPatch, values: dict[str, Any]) -> None:
    """Set each value explicitly: never rely on a default a CI variable could change."""
    settings = get_settings()
    for key, value in values.items():
        if key == "SLEEP_LLM_FORCE_ENV":
            monkeypatch.setenv(key, "1" if value else "0")
        else:
            monkeypatch.setattr(settings, key.lower(), value)


def _assert_category_answered(client, mode, monkeypatch, category: str) -> None:
    values = NEW_CATEGORY_VALUES[category]
    _set_effective(monkeypatch, values)
    schema = config_routes.get_config_schema()

    mode("session")
    _as(ADMIN_SESSION)
    session_response = client.get(URL)
    session_items = _items(session_response)

    token = secrets.token_hex(32)
    mode("service", system_admin_service_token=token)
    _as(None)
    service_response = client.get(
        URL, headers={"Authorization": f"Bearer {token}", "X-Operator-Email": OPERATOR}
    )
    service_items = _items(service_response)

    for items in (session_items, service_items):
        answered = {key: item for key, item in items.items() if item["category"] == category}
        assert list(answered) == list(values)
        assert {key: item["value"] for key, item in answered.items()} == values
        for key, item in answered.items():
            assert item["value"] != MARKER, key
            assert item["type"] == schema[key].type, key
            assert item["requires_restart"] is True, key
            assert item["description"] == schema[key].description, key
    assert session_response.json() == service_response.json()


def test_signup_category_answers_its_effective_values(client, mode, monkeypatch) -> None:
    _assert_category_answered(client, mode, monkeypatch, "signup")


def test_rewards_category_answers_its_effective_values(client, mode, monkeypatch) -> None:
    _assert_category_answered(client, mode, monkeypatch, "rewards")


def test_operations_category_answers_its_effective_values(client, mode, monkeypatch) -> None:
    _assert_category_answered(client, mode, monkeypatch, "operations")


def test_sleep_llm_force_env_unset_answers_false(admin, monkeypatch) -> None:
    monkeypatch.delenv("SLEEP_LLM_FORCE_ENV", raising=False)
    assert _items(admin.get(URL))["SLEEP_LLM_FORCE_ENV"]["value"] is False


def test_no_new_key_is_sensitive_or_answered_masked(admin, monkeypatch) -> None:
    from api.routes.admin_environment import ANSWERED_KEYS

    new_keys = {key for values in NEW_CATEGORY_VALUES.values() for key in values}
    assert new_keys <= ANSWERED_KEYS
    assert new_keys & config_routes.get_sensitive_keys() == set()
    # the values at their defaults too: a False or 0 is answered, not withheld
    response = admin.get(URL)
    items = _items(response)
    for key in new_keys:
        assert items[key]["value"] != MARKER, key
    assert MARKER not in response.text


# --- #2958: HYBRID_FUSION ------------------------------------------------------------


@pytest.mark.parametrize("fusion", ["weighted", "rrf", "keyword_floor"])
def test_hybrid_fusion_is_answered_in_the_search_category(admin, monkeypatch, fusion) -> None:
    from api.routes.admin_environment import ANSWERED_KEYS

    monkeypatch.setattr(get_settings(), "hybrid_fusion", fusion)
    item = _items(admin.get(URL))["HYBRID_FUSION"]
    assert item["category"] == "search"
    assert item["value"] == fusion
    assert item["type"] == "enum"
    assert item["requires_restart"] is True
    assert "HYBRID_FUSION" in ANSWERED_KEYS
    schema = config_routes.get_config_schema()["HYBRID_FUSION"]
    assert schema.enum_values == ["weighted", "rrf", "keyword_floor"]
    assert schema.default_value == "weighted"
    assert schema.is_sensitive is False
