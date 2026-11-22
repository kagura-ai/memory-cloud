"""API tests for the read-only environment console routes (#1580).

Every key exposed by ``get_config_categories()`` is env-backed and nothing at
runtime reads ``config_overrides``. So ``GET /config`` must render the
EFFECTIVE value (never a stored override row) and the write routes must refuse
instead of storing a value that changes no behaviour.

Auth and DB are mocked via dependency_overrides; the DB mock carries a
``ConfigOverride``-shaped row so a regression back to "override wins" fails.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routes.config import get_config_categories, get_config_schema, mask_url_credentials
from auth.dependencies import (
    get_current_user,
    get_current_user_optional,
    get_user_from_api_key_or_session,
)
from config.settings import Settings
from db.base import get_db

HOSTED_KEYS = [
    "ENABLE_BYOK",
    "RESOLVE_STORED_BYOK_KEYS",
    "ENABLE_COST_DISPLAY",
    "ENABLE_PLAN_PAGE",
    "MANAGED_LLM_PROVIDER",
    "MANAGED_LLM_MODEL",
    "DEFAULT_USE_RERANK",
    "DEFAULT_RERANKER_PROVIDER",
    "DEFAULT_RERANKER_MODEL",
    "RERANK_BASE_URL",
    "RERANK_MODEL",
]

# #2954: the admin-only categories added after ``hosted``.
NEW_ADMIN_CATEGORIES = {
    "signup": [
        "ALLOW_REGISTRATION",
        "ENABLE_EMAIL_SIGNUP",
        "ENABLE_PROVIDER_LINK_CONFIRMATION",
        "ENABLE_BETA_INVITES",
        "BETA_INVITE_QUOTA_PER_USER",
    ],
    "rewards": [
        "ENABLE_REFERRALS",
        "REFERRAL_REFEREE_REWARD_MEMORIES",
        "REFERRAL_REFERRER_REWARD_MEMORIES",
        "REFERRAL_MAX_GRANTS_PER_REFERRER",
        "REFERRAL_REDEEM_WINDOW_HOURS",
    ],
    "operations": [
        "MCP_NOTICES_ENABLED",
        "REQUIRE_DUAL_CONTROL_FORCE_TRANSFER",
        "BM25_DRIFT_CRON_ENABLED",
        "BM25_REVEAL_RATE_LIMIT_PER_HOUR",
        "OPERATIONS_PROBE_INTERVAL_SECONDS",
        "FILE_SCAN_ENABLED",
        "SLEEP_LLM_FORCE_ENV",
    ],
}
NEW_ADMIN_KEYS = [key for keys in NEW_ADMIN_CATEGORIES.values() for key in keys]

# Env that would leak into a fresh Settings / NeuralMemoryConfig.from_env().
_ENV_KEYS = [
    *HOSTED_KEYS,
    *NEW_ADMIN_KEYS,
    "ENABLE_RERANKING",
    "HYBRID_FUSION",
    "SELF_HOSTED_BASE_URL",
    "TRACK_CO_ACTIVATION",
    "ENABLE_DECAY",
    "ENABLE_TRUST_MODULATION",
]


def _user(role: str) -> dict:
    return {"user_id": f"{role}_user_1", "email": f"{role}@test.com", "role": role}


@pytest.fixture
def db():
    """A session whose only content is a stale ENABLE_RERANKING override row."""
    override = MagicMock(key="ENABLE_RERANKING", value="False")
    result = MagicMock()
    result.scalars.return_value.all.return_value = [override]
    result.scalar_one_or_none.return_value = override
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


@pytest.fixture
def client_as(db, monkeypatch):
    """TestClient factory: ``client_as("admin", **settings_overrides)``."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    def _client(role: str = "admin", **overrides) -> TestClient:
        settings = Settings(_env_file=None, **overrides)
        monkeypatch.setattr("api.routes.config.get_settings", lambda: settings)

        async def mock_user():
            return _user(role)

        async def mock_db():
            yield db

        app.dependency_overrides[get_user_from_api_key_or_session] = mock_user
        app.dependency_overrides[get_current_user] = mock_user
        # #2767: require_admin reads the session through get_current_user_optional.
        app.dependency_overrides[get_current_user_optional] = mock_user
        app.dependency_overrides[get_db] = mock_db
        return TestClient(app, raise_server_exceptions=False)

    yield _client
    app.dependency_overrides.clear()


def _values(resp) -> dict:
    assert resp.status_code == 200, resp.text
    return {item["key"]: item for item in resp.json()["configs"]}


class TestGetConfig:
    def test_effective_value_wins_over_a_stored_override_row(self, client_as, db):
        """The console must show what the process runs with, not a saved row."""
        configs = _values(client_as(enable_reranking=True).get("/api/v1/config"))

        assert configs["ENABLE_RERANKING"]["value"] is True
        # The overrides table is not even consulted.
        db.execute.assert_not_called()

    def test_every_item_is_read_only_and_keeps_the_legacy_fields(self, client_as):
        body = client_as().get("/api/v1/config").json()

        assert body["total"] == len(body["configs"])
        for item in body["configs"]:
            assert item["read_only"] is True, item["key"]
            # Older frontends read exactly these fields.
            assert {"key", "value", "category", "description", "is_sensitive"} <= set(item)

    def test_hosted_mode_settings_are_visible_to_an_admin(self, client_as):
        client = client_as(
            enable_byok=False,
            resolve_stored_byok_keys=False,
            enable_cost_display=False,
            enable_plan_page=True,
            managed_llm_provider="self_hosted",
            managed_llm_model="qwen3",
            # Boot validation: a self_hosted managed lane needs an explicit backend.
            self_hosted_base_url="http://inference:8000/v1",
            default_use_rerank=True,
            default_reranker_provider="self_hosted",
            default_reranker_model="bge-reranker-v2-m3",
            rerank_base_url="http://reranker:8000",
            rerank_model="bge-reranker-v2-m3",
        )
        configs = _values(client.get("/api/v1/config"))

        assert {k: configs[k]["value"] for k in HOSTED_KEYS} == {
            "ENABLE_BYOK": False,
            "RESOLVE_STORED_BYOK_KEYS": False,
            "ENABLE_COST_DISPLAY": False,
            "ENABLE_PLAN_PAGE": True,
            "MANAGED_LLM_PROVIDER": "self_hosted",
            "MANAGED_LLM_MODEL": "qwen3",
            "DEFAULT_USE_RERANK": True,
            "DEFAULT_RERANKER_PROVIDER": "self_hosted",
            "DEFAULT_RERANKER_MODEL": "bge-reranker-v2-m3",
            "RERANK_BASE_URL": "http://reranker:8000",
            "RERANK_MODEL": "bge-reranker-v2-m3",
        }
        assert {configs[k]["category"] for k in HOSTED_KEYS} == {"hosted"}

    def test_hosted_mode_settings_are_hidden_from_a_non_admin(self, client_as):
        """GET /config is open to any authenticated caller; the deployment
        posture (managed LLM, internal reranker URL) is operator-only."""
        configs = _values(client_as("member").get("/api/v1/config"))

        assert not set(HOSTED_KEYS) & set(configs)
        assert "ENABLE_RERANKING" in configs  # the pre-existing keys are unchanged

    @pytest.mark.parametrize(("role", "listed"), [("admin", True), ("member", False)])
    def test_categories_list_the_hosted_category_for_admins_only(self, client_as, role, listed):
        resp = client_as(role).get("/api/v1/config/categories")

        assert resp.status_code == 200
        assert ("hosted" in resp.json()["categories"]) is listed

    def test_the_new_categories_follow_hosted_with_their_keys(self):
        categories = get_config_categories()
        assert list(categories) == [
            "embedding",
            "search",
            "system",
            "hosted",
            "signup",
            "rewards",
            "operations",
        ]
        for category, keys in NEW_ADMIN_CATEGORIES.items():
            assert categories[category] == keys, category

    @pytest.mark.parametrize("category", list(NEW_ADMIN_CATEGORIES))
    def test_new_admin_categories_are_hidden_from_a_non_admin(self, client_as, category):
        configs = _values(client_as("member").get("/api/v1/config"))

        assert not set(NEW_ADMIN_CATEGORIES[category]) & set(configs)
        assert category not in {item["category"] for item in configs.values()}
        assert "ENABLE_RERANKING" in configs

    @pytest.mark.parametrize("category", list(NEW_ADMIN_CATEGORIES))
    def test_new_admin_categories_are_visible_to_an_admin(self, client_as, category):
        configs = _values(client_as().get("/api/v1/config"))

        keys = NEW_ADMIN_CATEGORIES[category]
        assert {k: configs[k]["category"] for k in keys} == dict.fromkeys(keys, category)
        for key in NEW_ADMIN_CATEGORIES[category]:
            assert configs[key]["is_sensitive"] is False, key

    @pytest.mark.parametrize("category", list(NEW_ADMIN_CATEGORIES))
    @pytest.mark.parametrize(("role", "listed"), [("admin", True), ("member", False)])
    def test_categories_list_the_new_categories_for_admins_only(
        self, client_as, role, listed, category
    ):
        resp = client_as(role).get("/api/v1/config/categories")

        assert resp.status_code == 200
        assert (category in resp.json()["categories"]) is listed

    def test_new_admin_keys_answer_their_effective_values(self, client_as):
        client = client_as(
            allow_registration=True,
            enable_email_signup=True,
            enable_provider_link_confirmation=False,
            enable_beta_invites=True,
            beta_invite_quota_per_user=7,
            enable_referrals=True,
            referral_referee_reward_memories=123,
            referral_referrer_reward_memories=456,
            referral_max_grants_per_referrer=9,
            referral_redeem_window_hours=48,
            mcp_notices_enabled=True,
            require_dual_control_force_transfer=True,
            bm25_drift_cron_enabled=True,
            bm25_reveal_rate_limit_per_hour=17,
            operations_probe_interval_seconds=600,
            file_scan_enabled=True,
        )
        configs = _values(client.get("/api/v1/config?mask_sensitive=true"))

        assert {k: configs[k]["value"] for k in NEW_ADMIN_KEYS} == {
            "ALLOW_REGISTRATION": True,
            "ENABLE_EMAIL_SIGNUP": True,
            "ENABLE_PROVIDER_LINK_CONFIRMATION": False,
            "ENABLE_BETA_INVITES": True,
            "BETA_INVITE_QUOTA_PER_USER": 7,
            "ENABLE_REFERRALS": True,
            "REFERRAL_REFEREE_REWARD_MEMORIES": 123,
            "REFERRAL_REFERRER_REWARD_MEMORIES": 456,
            "REFERRAL_MAX_GRANTS_PER_REFERRER": 9,
            "REFERRAL_REDEEM_WINDOW_HOURS": 48,
            "MCP_NOTICES_ENABLED": True,
            "REQUIRE_DUAL_CONTROL_FORCE_TRANSFER": True,
            "BM25_DRIFT_CRON_ENABLED": True,
            "BM25_REVEAL_RATE_LIMIT_PER_HOUR": 17,
            "OPERATIONS_PROBE_INTERVAL_SECONDS": 600,
            "FILE_SCAN_ENABLED": True,
            "SLEEP_LLM_FORCE_ENV": False,
        }

    @pytest.mark.parametrize("raw", ["1", "true", "YES", " yes "])
    def test_sleep_llm_force_env_answers_true(self, client_as, monkeypatch, raw):
        client = client_as()
        monkeypatch.setenv("SLEEP_LLM_FORCE_ENV", raw)

        assert _values(client.get("/api/v1/config"))["SLEEP_LLM_FORCE_ENV"]["value"] is True

    @pytest.mark.parametrize("raw", [None, "", "0", "false", "no", "on"])
    def test_sleep_llm_force_env_answers_false(self, client_as, monkeypatch, raw):
        client = client_as()
        if raw is not None:
            monkeypatch.setenv("SLEEP_LLM_FORCE_ENV", raw)

        assert _values(client.get("/api/v1/config"))["SLEEP_LLM_FORCE_ENV"]["value"] is False

    @pytest.mark.parametrize("mask_sensitive", ["true", "false"])
    def test_rerank_base_url_never_shows_embedded_credentials(self, client_as, mask_sensitive):
        client = client_as(rerank_base_url="https://svc:s3cr3t@reranker.internal:8443/v1")
        configs = _values(client.get(f"/api/v1/config?mask_sensitive={mask_sensitive}"))

        assert configs["RERANK_BASE_URL"]["value"] == "https://***@reranker.internal:8443/v1"

    def test_neural_env_flags_render_their_effective_default(self, client_as):
        """These are not Settings fields — unset used to render as null (shown
        as "Disabled") while the neural layer ran with its default of true."""
        configs = _values(client_as().get("/api/v1/config"))

        for key in ("TRACK_CO_ACTIVATION", "ENABLE_DECAY", "ENABLE_TRUST_MODULATION"):
            assert configs[key]["value"] is True, key

    def test_neural_env_flags_follow_the_environment(self, client_as, monkeypatch):
        client = client_as()
        monkeypatch.setenv("ENABLE_DECAY", "false")

        assert _values(client.get("/api/v1/config"))["ENABLE_DECAY"]["value"] is False

    def test_every_exposed_key_resolves(self, client_as):
        configs = _values(client_as().get("/api/v1/config"))

        exposed = [k for keys in get_config_categories().values() for k in keys]
        assert sorted(configs) == sorted(exposed)


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("", ""),
        ("http://reranker:8000/v1", "http://reranker:8000/v1"),
        ("https://svc:s3cr3t@reranker:8443/v1", "https://***@reranker:8443/v1"),
        ("https://token@[::1]:8443", "https://***@[::1]:8443"),
        # Fail closed on shapes a URL parser would not see as userinfo.
        ("svc:s3cr3t@reranker:8000", "***@reranker:8000"),
        ("https://svc:a://b@reranker", "https://***@reranker"),
        ("https://svc:p@ss@reranker", "https://***@reranker"),
    ],
)
def test_mask_url_credentials(raw, shown):
    assert mask_url_credentials(raw) == shown


class TestWritesAreRefused:
    def test_put_is_refused_with_409_and_writes_nothing(self, client_as, db):
        resp = client_as().put("/api/v1/config/ENABLE_RERANKING", json={"value": False})

        assert resp.status_code == 409
        body = resp.json()
        assert body["error"] == "CFG-002"
        assert "environment" in body["message"]
        assert "restart" in body["message"]
        assert body["details"] == {"keys": ["ENABLE_RERANKING"]}
        db.add.assert_not_called()
        db.commit.assert_not_called()

    def test_batch_is_refused_as_a_whole(self, client_as, db):
        resp = client_as().post(
            "/api/v1/config/batch",
            json={"updates": {"LOG_LEVEL": "DEBUG", "ENABLE_RERANKING": False}},
        )

        assert resp.status_code == 409
        body = resp.json()
        assert body["error"] == "CFG-002"
        assert body["details"] == {"keys": ["ENABLE_RERANKING", "LOG_LEVEL"]}
        db.add.assert_not_called()
        db.commit.assert_not_called()

    @pytest.mark.parametrize(
        ("method", "path", "payload"),
        [
            ("PUT", "/api/v1/config/ENABLE_RERANKING", {"value": False}),
            ("POST", "/api/v1/config/batch", {"updates": {"ENABLE_RERANKING": False}}),
        ],
    )
    def test_write_routes_stay_admin_only(self, client_as, method, path, payload):
        """The refusal must not turn the routes into a probe for non-admins."""
        resp = client_as("member").request(method, path, json=payload)

        assert resp.status_code == 403


class TestSchema:
    def test_exposed_keys_truthfully_require_a_restart(self):
        """Env is read at process start: no exposed key applies without one."""
        schema = get_config_schema()
        exposed = [k for keys in get_config_categories().values() for k in keys]

        for key in exposed:
            assert key in schema, key
            assert schema[key].requires_restart is True, key

    def test_hosted_mode_keys_have_schema_metadata(self):
        schema = get_config_schema()

        for key in HOSTED_KEYS:
            assert schema[key].category == "hosted", key

    def test_new_admin_keys_have_schema_metadata(self):
        schema = get_config_schema()

        for category, keys in NEW_ADMIN_CATEGORIES.items():
            for key in keys:
                assert schema[key].category == category, key
                assert schema[key].type in ("boolean", "number"), key
                assert schema[key].description, key
                assert schema[key].is_sensitive is False, key
        assert schema["SLEEP_LLM_FORCE_ENV"].type == "boolean"
        assert schema["SLEEP_LLM_FORCE_ENV"].default_value is False

    def test_schema_types_and_bounds_match_the_settings_fields(self):
        """Booleans are ``boolean``; integers are ``number`` with the Field's bounds."""
        import annotated_types

        schema = get_config_schema()
        for key in NEW_ADMIN_KEYS:
            field = Settings.model_fields.get(key.lower())
            if field is None:
                continue
            if field.annotation is bool:
                assert schema[key].type == "boolean", key
                assert (schema[key].min_value, schema[key].max_value) == (None, None), key
                continue
            assert field.annotation is int, key
            assert schema[key].type == "number", key
            ge = next((m.ge for m in field.metadata if isinstance(m, annotated_types.Ge)), None)
            le = next((m.le for m in field.metadata if isinstance(m, annotated_types.Le)), None)
            assert (schema[key].min_value, schema[key].max_value) == (ge, le), key

    def test_schema_defaults_match_the_settings_defaults(self):
        """Drift guard: the console's default_value is the Settings default."""
        schema = get_config_schema()
        exposed = [k for keys in get_config_categories().values() for k in keys]

        for key in exposed:
            field = Settings.model_fields.get(key.lower())
            if key in schema and field is not None:
                assert schema[key].default_value == field.default, key
