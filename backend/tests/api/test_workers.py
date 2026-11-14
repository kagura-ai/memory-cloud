"""Tests for the ai-worker config endpoint (Spec 2026-06-02, Plan 3)."""

from datetime import timedelta
from json import JSONDecodeError
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from cryptography.fernet import InvalidToken
from fastapi import Response
from starlette.requests import Request

import api.routes.workers as workers_module
from api.routes.workers import (
    WorkerConnectorConfig,
    get_worker_apps,
    get_worker_config,
    verify_worker_token,
)
from utils.datetime import utcnow

# #1939: the real check, kept before the autouse stub below replaces it.
_REAL_SUSPENSION_CHECK = getattr(workers_module, "_connectors_suspended_for", None)


@pytest.fixture(autouse=True)
def _connectors_not_suspended():
    """Paid plan by default; the #1939 suspension tests restore the real check."""
    with patch("api.routes.workers._connectors_suspended_for", new=AsyncMock(return_value=False)):
        yield


@pytest.fixture(autouse=True)
def _default_context_live():
    """The default context is live unless a test says otherwise (the real
    lookup is covered below and DB-tested in test_1759_channel_routes_db.py)."""
    with patch("api.routes.workers._default_context_live", new=AsyncMock(return_value=True)):
        yield


@pytest.fixture(autouse=True)
def _no_channel_routing():
    """#1759: no routes and the switch at ``default`` unless a test says
    otherwise (the real read is DB-tested in
    ``tests/integration/test_1759_channel_routes_db.py``)."""
    from services.connector_channel_routes import NO_ROUTING

    with patch("api.routes.workers._channel_routing_for", new=AsyncMock(return_value=NO_ROUTING)):
        yield


# #1447: the minimum bundle the bridge's ``LLMConfig`` accepts. A connector
# missing any of these cannot vend, so the readiness gate rejects it.
_VENDABLE_LLM = {"provider": "anthropic", "model": "m", "api_key": "sk"}


def _request(*headers: tuple[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(name.lower().encode(), value.encode()) for name, value in headers],
        }
    )


def _settings(
    token: str = "wt-secret",
    mcp_url: str = "https://mcp.example/mcp",
    managed_connectors: bool = False,
):
    return SimpleNamespace(
        worker_service_token=token,
        kmc_mcp_url=mcp_url,
        # #1447: mirrors the production default (OSS / self-host, BYO LLM).
        enable_managed_connectors=managed_connectors,
    )


def test_worker_config_omits_only_absent_runtime_block():
    config = WorkerConnectorConfig(
        connector_id=uuid4(),
        workspace_id=uuid4(),
        context_id=uuid4(),
        platform="slack",
        app_key="default",
        config_revision="revision",
        slack={},
        kmc={},
    )

    payload = config.model_dump(mode="json")

    assert "runtime" not in payload
    assert payload["locale"] is None
    assert payload["resource"] is None
    assert payload["llm"] is None
    assert payload["pii_guardrail_config"] is None


@pytest.mark.asyncio
async def test_verify_worker_token_accepts_matching_bearer():
    with patch("api.routes.workers.get_settings", return_value=_settings()):
        # Returns None (no raise) on a valid token.
        assert await verify_worker_token(_request(), "Bearer wt-secret") is None


@pytest.mark.asyncio
async def test_verify_worker_token_rejects_wrong_token():
    from fastapi import HTTPException

    with patch("api.routes.workers.get_settings", return_value=_settings()):
        with pytest.raises(HTTPException) as exc:
            await verify_worker_token(_request(), "Bearer nope")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_worker_token_503_when_unconfigured():
    from fastapi import HTTPException

    with patch("api.routes.workers.get_settings", return_value=_settings(token="")):
        with pytest.raises(HTTPException) as exc:
            await verify_worker_token(_request(), "Bearer anything")
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_verify_worker_token_rejects_duplicate_authorization_headers():
    from utils.exceptions import AuthenticationError

    with patch("api.routes.workers.get_settings", return_value=_settings()):
        with pytest.raises(AuthenticationError) as exc:
            await verify_worker_token(
                _request(
                    ("Authorization", "Bearer wt-secret"),
                    ("Authorization", "Bearer wt-secret"),
                ),
                "Bearer wt-secret",
            )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_get_worker_config_returns_secrets_for_ready_connector():
    db = MagicMock()
    conn = MagicMock()
    conn.id = uuid4()
    conn.workspace_id = uuid4()
    conn.context_id = uuid4()
    conn.connector_type = "slack"
    conn.locale = "ja"
    conn.external_team_id = "T01"
    conn.config_version = 1
    conn.channel_ids = ["C01"]
    conn.pii_guardrail_config = {"enabled": True}
    conn.runtime_config = {"vision_enabled": False}
    conn.get_oauth_tokens.return_value = {
        "bot_token": "xoxb-x",
        "installing_admin_user_id": "U01",
    }
    conn.get_kmc_api_key.return_value = "kagura_writekey"
    # #892: expiry check reads this; None = non-expiring (avoids MagicMock < datetime)
    conn.kmc_api_key_expires_at = None
    # #895: no stored resource token → resource block skipped (avoids awaiting a
    # MagicMock db.execute for the slug lookup). Covered separately below.
    conn.get_resource_token.return_value = None
    conn.get_llm_config.return_value = {"provider": "anthropic", "model": "m", "api_key": "sk"}

    with (
        patch("api.routes.workers.get_settings", return_value=_settings()),
        patch("api.routes.workers.ConnectorProvisioningService") as svc,
        patch("api.routes.workers.WorkerAppIdentityService") as app_svc,
    ):
        app_svc.return_value.get_identity = AsyncMock(return_value=None)
        svc.return_value.get_connector_for_dispatch = AsyncMock(return_value=conn)
        result = await get_worker_config(
            response=Response(),
            platform="slack",
            team_id="T01",
            app_key=None,
            if_none_match=None,
            _=None,
            db=db,
        )

    assert result.connector_id == conn.id
    assert result.slack["bot_token"] == "xoxb-x"
    assert result.slack["team_id"] == "T01"
    assert result.slack["channel_ids"] == ["C01"]
    assert result.kmc == {"mcp_url": "https://mcp.example/mcp", "api_key": "kagura_writekey"}
    assert result.llm["api_key"] == "sk"
    assert result.runtime is not None
    assert result.runtime.vision_enabled is False
    assert result.runtime.buffer.ttl_seconds == 86400
    svc.return_value.get_connector_for_dispatch.assert_awaited_once_with(
        connector_type="slack", external_team_id="T01", app_key="default"
    )
    # #895: legacy connector (no stored resource token) → resource omitted.
    assert result.resource is None


def _minimal_ready_conn(locale):
    """MagicMock connector with just enough state for a config vend (#1377)."""
    conn = MagicMock()
    conn.id = uuid4()
    conn.workspace_id = uuid4()
    conn.context_id = uuid4()
    conn.connector_type = "slack"
    conn.locale = locale
    conn.external_team_id = "T01"
    conn.config_version = 1
    conn.channel_ids = ["C01"]
    conn.pii_guardrail_config = None
    conn.runtime_config = None
    conn.get_oauth_tokens.return_value = {"bot_token": "xoxb-x"}
    conn.get_kmc_api_key.return_value = "kagura_writekey"
    conn.kmc_api_key_expires_at = None
    conn.get_resource_token.return_value = None
    # #1447: "ready" now includes a vendable LLM bundle — a connector without
    # one is not-ready and never reaches the locale/runtime assertions below.
    conn.get_llm_config.return_value = dict(_VENDABLE_LLM)
    return conn


async def _vend_config_for(conn):
    with (
        patch("api.routes.workers.get_settings", return_value=_settings()),
        patch("api.routes.workers.ConnectorProvisioningService") as svc,
        patch("api.routes.workers.WorkerAppIdentityService") as app_svc,
    ):
        app_svc.return_value.get_identity = AsyncMock(return_value=None)
        svc.return_value.get_connector_for_dispatch = AsyncMock(return_value=conn)
        return await get_worker_config(
            response=Response(),
            platform="slack",
            team_id="T01",
            app_key=None,
            if_none_match=None,
            _=None,
            db=MagicMock(),
        )


@pytest.mark.asyncio
async def test_get_worker_config_normalizes_legacy_bcp47_locale():
    """#1377: a pre-fix row storing ja-JP is vended as the contract value ja."""
    result = await _vend_config_for(_minimal_ready_conn("ja-JP"))
    assert result.locale == "ja"


@pytest.mark.asyncio
async def test_get_worker_config_nonconforming_locale_vends_none():
    """#1377: a non-conforming legacy locale must not fail the tenant closed —
    it degrades to None (worker default) instead of failing bridge-side
    validation of the whole config body."""
    result = await _vend_config_for(_minimal_ready_conn("zz-XX"))
    assert result.locale is None


@pytest.mark.asyncio
async def test_get_worker_config_revision_changes_when_runtime_revision_changes():
    db = MagicMock()
    conn = MagicMock()
    conn.id = uuid4()
    conn.workspace_id = uuid4()
    conn.context_id = uuid4()
    conn.connector_type = "slack"
    conn.locale = "ja"
    conn.external_team_id = "T01"
    conn.config_version = 1
    conn.channel_ids = []
    conn.pii_guardrail_config = None
    conn.runtime_config = {"vision_enabled": True}
    conn.get_oauth_tokens.return_value = {"bot_token": "xoxb-x"}
    conn.get_kmc_api_key.return_value = "kagura_writekey"
    conn.kmc_api_key_expires_at = None
    conn.get_resource_token.return_value = None
    conn.get_llm_config.return_value = dict(_VENDABLE_LLM)

    with (
        patch("api.routes.workers.get_settings", return_value=_settings()),
        patch("api.routes.workers.ConnectorProvisioningService") as svc,
        patch("api.routes.workers.WorkerAppIdentityService") as app_svc,
    ):
        app_svc.return_value.get_identity = AsyncMock(return_value=None)
        svc.return_value.get_connector_for_dispatch = AsyncMock(return_value=conn)
        first_response = Response()
        first = await get_worker_config(
            response=first_response,
            platform="slack",
            team_id="T01",
            app_key=None,
            if_none_match=None,
            _=None,
            db=db,
        )

        conn.config_version = 2
        conn.runtime_config = {"vision_enabled": False}
        second_response = Response()
        second = await get_worker_config(
            response=second_response,
            platform="slack",
            team_id="T01",
            app_key=None,
            if_none_match=None,
            _=None,
            db=db,
        )

    assert first.config_revision != second.config_revision
    assert first_response.headers["etag"] != second_response.headers["etag"]
    assert second.runtime is not None
    assert second.runtime.vision_enabled is False


@pytest.mark.asyncio
async def test_get_worker_config_includes_resource_block_when_token_present():
    # #895: a connector with a stored resource token returns the resource block
    # {id, api_key} for the resource-ingest write path.
    db = MagicMock()
    slug_result = MagicMock()
    slug_result.scalar_one_or_none.return_value = "slack_general"
    db.execute = AsyncMock(return_value=slug_result)

    conn = MagicMock()
    conn.id = uuid4()
    conn.workspace_id = uuid4()
    conn.context_id = uuid4()
    conn.connector_type = "slack"
    conn.locale = "ja"
    conn.external_team_id = "T01"
    conn.config_version = 1
    conn.channel_ids = ["C01"]
    conn.pii_guardrail_config = None
    conn.runtime_config = None
    conn.resource_pk = uuid4()
    conn.get_oauth_tokens.return_value = {"bot_token": "xoxb-x"}
    conn.get_kmc_api_key.return_value = "kagura_writekey"
    conn.kmc_api_key_expires_at = None
    conn.get_resource_token.return_value = "kgr_resource_token"
    conn.get_llm_config.return_value = dict(_VENDABLE_LLM)

    with (
        patch("api.routes.workers.get_settings", return_value=_settings()),
        patch("api.routes.workers.ConnectorProvisioningService") as svc,
        patch("api.routes.workers.WorkerAppIdentityService") as app_svc,
    ):
        app_svc.return_value.get_identity = AsyncMock(return_value=None)
        svc.return_value.get_connector_for_dispatch = AsyncMock(return_value=conn)
        result = await get_worker_config(
            response=Response(),
            platform="slack",
            team_id="T01",
            app_key=None,
            if_none_match=None,
            _=None,
            db=db,
        )

    assert result.resource == {"id": "slack_general", "api_key": "kgr_resource_token"}


@pytest.mark.asyncio
async def test_get_worker_config_404_when_not_found():
    from utils.exceptions import WorkerConnectorNotReadyError

    db = MagicMock()
    with (
        patch("api.routes.workers.get_settings", return_value=_settings()),
        patch("api.routes.workers.ConnectorProvisioningService") as svc,
        patch("api.routes.workers.WorkerAppIdentityService") as app_svc,
    ):
        app_svc.return_value.get_identity = AsyncMock(return_value=None)
        svc.return_value.get_connector_for_dispatch = AsyncMock(return_value=None)
        with pytest.raises(WorkerConnectorNotReadyError) as exc:
            await get_worker_config(
                response=Response(),
                platform="slack",
                team_id="TX",
                app_key=None,
                if_none_match=None,
                _=None,
                db=db,
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_worker_config_404_when_context_not_ready():
    from utils.exceptions import WorkerConnectorNotReadyError

    db = MagicMock()
    conn = MagicMock()
    conn.context_id = None  # registration incomplete
    with (
        patch("api.routes.workers.get_settings", return_value=_settings()),
        patch("api.routes.workers.ConnectorProvisioningService") as svc,
        patch("api.routes.workers.WorkerAppIdentityService") as app_svc,
    ):
        app_svc.return_value.get_identity = AsyncMock(return_value=None)
        svc.return_value.get_connector_for_dispatch = AsyncMock(return_value=conn)
        with pytest.raises(WorkerConnectorNotReadyError) as exc:
            await get_worker_config(
                response=Response(),
                platform="slack",
                team_id="T01",
                app_key=None,
                if_none_match=None,
                _=None,
                db=db,
            )
    assert exc.value.status_code == 404


# What a failed secret read actually looks like. ``Encryptor.decrypt`` wraps
# Fernet's ``InvalidToken`` into a ``ValueError`` (utils/encryption.py), so that
# is the real shape; the bare ``InvalidToken`` case guards the readiness
# boundary against that wrapping ever going away, and ``JSONDecodeError`` covers
# a decryptable-but-unparseable document.
_DECRYPT_FAILURES = [
    pytest.param(ValueError("Invalid encrypted value or wrong encryption key"), id="wrapped"),
    pytest.param(InvalidToken(), id="raw-invalid-token"),
    pytest.param(JSONDecodeError("Expecting value", "", 0), id="unparseable"),
]


def _llm_gate_conn(llm_config):
    """A connector complete in every respect except its LLM bundle (#1447)."""
    conn = MagicMock()
    conn.id = uuid4()
    conn.workspace_id = uuid4()
    conn.context_id = uuid4()
    conn.connector_type = "slack"
    conn.locale = None
    conn.external_team_id = "T01"
    conn.config_version = 1
    conn.channel_ids = ["C01"]
    conn.pii_guardrail_config = None
    conn.runtime_config = None
    conn.get_oauth_tokens.return_value = {"bot_token": "xoxb-x"}
    conn.get_kmc_api_key.return_value = "kagura_writekey"
    conn.kmc_api_key_expires_at = None
    conn.get_resource_token.return_value = None
    conn.get_llm_config.return_value = llm_config
    return conn


async def _vend_with_llm(llm_config, *, managed: bool = False):
    with (
        patch(
            "api.routes.workers.get_settings",
            return_value=_settings(managed_connectors=managed),
        ),
        patch("api.routes.workers.ConnectorProvisioningService") as svc,
        patch("api.routes.workers.WorkerAppIdentityService") as app_svc,
    ):
        app_svc.return_value.get_identity = AsyncMock(return_value=None)
        svc.return_value.get_connector_for_dispatch = AsyncMock(
            return_value=_llm_gate_conn(llm_config)
        )
        return await get_worker_config(
            response=Response(),
            platform="slack",
            team_id="T01",
            app_key=None,
            if_none_match=None,
            _=None,
            db=MagicMock(),
        )


@pytest.mark.asyncio
async def test_get_worker_config_404_when_llm_config_absent():
    """#1447: ``llm: null`` used to vend 200; the worker then fails closed on
    every event and drops it, so the tenant loses data silently. Not-ready is
    the honest answer — the worker treats 404 as "skip", not as a failure."""
    from utils.exceptions import WorkerConnectorNotReadyError

    with pytest.raises(WorkerConnectorNotReadyError) as exc:
        await _vend_with_llm(None)
    assert exc.value.status_code == 404
    assert exc.value.error_code == "WORKER-CONNECTOR-001"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "llm_config",
    [
        pytest.param({"provider": "anthropic", "model": "m"}, id="no-api-key"),
        pytest.param({"provider": "anthropic", "model": "m", "api_key": "  "}, id="blank-api-key"),
        pytest.param({"provider": "anthropic", "api_key": "sk"}, id="no-model"),
        pytest.param({"model": "m", "api_key": "sk"}, id="no-provider"),
        pytest.param({}, id="empty"),
        pytest.param("anthropic", id="not-a-dict"),
    ],
)
async def test_get_worker_config_404_when_llm_config_cannot_vend(llm_config):
    """#1447: ``_validate_llm_config`` deliberately does not require ``api_key``
    (local ollama has none), but the bridge's ``LLMConfig`` does — so a bundle
    that stores fine is still un-vendable. The gate follows the bridge."""
    from utils.exceptions import WorkerConnectorNotReadyError

    with pytest.raises(WorkerConnectorNotReadyError) as exc:
        await _vend_with_llm(llm_config)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_worker_config_vends_without_llm_in_managed_mode():
    """#1447: managed SaaS (#1426) is the one deployment where ``llm: null`` is
    legitimate — the shared bridge supplies the pre-compile LLM (bridge #216).
    The gate must not fail those tenants closed."""
    result = await _vend_with_llm(None, managed=True)
    assert result.llm is None
    assert result.kmc["api_key"] == "kagura_writekey"


@pytest.mark.asyncio
async def test_get_worker_config_404_in_managed_mode_when_bundle_is_broken():
    """#1447: the managed carve-out covers an ABSENT bundle, not a broken one.
    A half-configured BYO bundle would fail the worker closed anyway; silently
    falling back to the shared LLM would hide the misconfiguration."""
    from utils.exceptions import WorkerConnectorNotReadyError

    with pytest.raises(WorkerConnectorNotReadyError) as exc:
        await _vend_with_llm({"provider": "anthropic", "model": "m"}, managed=True)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", _DECRYPT_FAILURES)
async def test_get_worker_config_404_when_llm_config_undecryptable(failure):
    """#1447: a bundle that cannot be decrypted or parsed (rotated Fernet key,
    corrupted ciphertext) must take the 404 path the worker already handles,
    not a 500 it has no rule for."""
    from utils.exceptions import WorkerConnectorNotReadyError

    conn = _llm_gate_conn(None)
    conn.get_llm_config.side_effect = failure

    with (
        patch("api.routes.workers.get_settings", return_value=_settings()),
        patch("api.routes.workers.ConnectorProvisioningService") as svc,
        patch("api.routes.workers.WorkerAppIdentityService") as app_svc,
    ):
        app_svc.return_value.get_identity = AsyncMock(return_value=None)
        svc.return_value.get_connector_for_dispatch = AsyncMock(return_value=conn)
        with pytest.raises(WorkerConnectorNotReadyError) as exc:
            await get_worker_config(
                response=Response(),
                platform="slack",
                team_id="T01",
                app_key=None,
                if_none_match=None,
                _=None,
                db=MagicMock(),
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", _DECRYPT_FAILURES)
async def test_get_worker_config_404_when_kmc_key_undecryptable(failure):
    """#1447 (review round 2): moving readiness above the 304 put the KMC
    decrypt on the cache-hit path too, so a rotated encryption key would have
    turned a previously-quiet 304 into a 500. A credential we cannot read is a
    credential the connector does not have — not-ready, like every other gate."""
    from utils.exceptions import WorkerConnectorNotReadyError

    conn = _llm_gate_conn(dict(_VENDABLE_LLM))
    conn.get_kmc_api_key.side_effect = failure

    with (
        patch("api.routes.workers.get_settings", return_value=_settings()),
        patch("api.routes.workers.ConnectorProvisioningService") as svc,
        patch("api.routes.workers.WorkerAppIdentityService") as app_svc,
    ):
        app_svc.return_value.get_identity = AsyncMock(return_value=None)
        svc.return_value.get_connector_for_dispatch = AsyncMock(return_value=conn)
        with pytest.raises(WorkerConnectorNotReadyError) as exc:
            await get_worker_config(
                response=Response(),
                platform="slack",
                team_id="T01",
                app_key=None,
                if_none_match=None,
                _=None,
                db=MagicMock(),
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_worker_config_404_beats_304_when_llm_becomes_unvendable():
    """#1447 (review): a matching ETag must not resurrect a poisoned config.

    ``config_revision`` covers ids + config_version only, so a readiness change
    that does not bump config_version — a deployment flipping
    ``enable_managed_connectors``, a rotated encryption key — leaves the worker
    holding a still-matching ETag. If readiness were evaluated after the
    conditional-GET short-circuit, that worker would keep its cached ``llm:
    null`` config forever and never see the 404 this fix exists to deliver.
    """
    from utils.exceptions import WorkerConnectorNotReadyError

    conn = _llm_gate_conn(dict(_VENDABLE_LLM))
    with (
        patch("api.routes.workers.get_settings", return_value=_settings()),
        patch("api.routes.workers.ConnectorProvisioningService") as svc,
        patch("api.routes.workers.WorkerAppIdentityService") as app_svc,
    ):
        app_svc.return_value.get_identity = AsyncMock(return_value=None)
        svc.return_value.get_connector_for_dispatch = AsyncMock(return_value=conn)
        ready = await get_worker_config(
            response=Response(),
            platform="slack",
            team_id="T01",
            app_key=None,
            if_none_match=None,
            _=None,
            db=MagicMock(),
        )

        # The bundle goes away without any config_version bump — the worker's
        # cached ETag still matches byte for byte.
        conn.get_llm_config.return_value = None

        with pytest.raises(WorkerConnectorNotReadyError) as exc:
            await get_worker_config(
                response=Response(),
                platform="slack",
                team_id="T01",
                app_key=None,
                if_none_match=f'"{ready.config_revision}"',
                _=None,
                db=MagicMock(),
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_worker_config_vends_complete_llm_bundle_unchanged():
    """#1447 regression guard: a ready connector's response is untouched."""
    result = await _vend_with_llm(dict(_VENDABLE_LLM))
    assert result.llm == _VENDABLE_LLM


@pytest.mark.asyncio
async def test_get_worker_config_uses_app_qualified_selector():
    db = MagicMock()
    identity = MagicMock()
    identity.status = "active"
    identity.config_version = 7
    identity.active_signing_secret_encrypted = "ciphertext"
    conn = MagicMock()
    conn.id = uuid4()
    conn.workspace_id = uuid4()
    conn.context_id = uuid4()
    conn.connector_type = "slack"
    conn.config_version = 3
    conn.locale = "en"
    conn.external_team_id = "T01"
    conn.channel_ids = []
    conn.pii_guardrail_config = None
    conn.runtime_config = None
    conn.kmc_api_key_expires_at = None
    conn.get_kmc_api_key.return_value = "kmc-key"
    conn.get_oauth_tokens.return_value = {"bot_token": "xoxb"}
    conn.get_resource_token.return_value = None
    conn.get_llm_config.return_value = dict(_VENDABLE_LLM)

    with (
        patch("api.routes.workers.WorkerAppIdentityService") as app_service,
        patch("api.routes.workers.ConnectorProvisioningService") as connector_service,
    ):
        app_service.return_value.get_identity = AsyncMock(return_value=identity)
        connector_service.return_value.get_connector_for_dispatch = AsyncMock(return_value=conn)
        result = await get_worker_config(
            response=Response(),
            platform="slack",
            team_id="T01",
            app_key="sales",
            if_none_match=None,
            _=None,
            db=db,
        )
        not_modified = await get_worker_config(
            response=Response(),
            platform="slack",
            team_id="T01",
            app_key="sales",
            if_none_match=f'"{result.config_revision}"',
            _=None,
            db=db,
        )

    assert result.app_key == "sales"
    assert not_modified.status_code == 304
    # #1447: was 1 — readiness now runs BEFORE the 304 short-circuit, so the key
    # is read on the cache-hit path too. That decrypt-per-poll is the deliberate
    # price of never answering 304 for a connector that has become not-ready
    # (see test_get_worker_config_404_beats_304_when_llm_becomes_unvendable).
    assert conn.get_kmc_api_key.call_count == 2
    connector_service.return_value.get_connector_for_dispatch.assert_awaited_with(
        connector_type="slack", external_team_id="T01", app_key="sales"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("app_key", "expected_app_key"), [("sales", "sales"), (None, "default")])
async def test_get_worker_config_disabled_app_is_explicit_gone(app_key, expected_app_key):
    from utils.exceptions import WorkerAppDisabledError

    identity = MagicMock(status="disabled")
    with (
        patch("api.routes.workers.WorkerAppIdentityService") as app_service,
        patch("api.routes.workers.ConnectorProvisioningService") as connector_service,
    ):
        app_service.return_value.get_identity = AsyncMock(return_value=identity)
        with pytest.raises(WorkerAppDisabledError) as exc:
            await get_worker_config(
                response=Response(),
                platform="slack",
                team_id="T01",
                app_key=app_key,
                if_none_match=None,
                _=None,
                db=MagicMock(),
            )
    assert exc.value.status_code == 410
    assert exc.value.error_code == "WORKER-APP-002"
    assert exc.value.details["app_key"] == expected_app_key
    connector_service.return_value.get_connector_for_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_get_worker_config_explicit_unknown_app_is_not_found():
    from utils.exceptions import WorkerAppNotFoundError

    with (
        patch("api.routes.workers.WorkerAppIdentityService") as app_service,
        patch("api.routes.workers.ConnectorProvisioningService") as connector_service,
    ):
        app_service.return_value.get_identity = AsyncMock(return_value=None)
        with pytest.raises(WorkerAppNotFoundError) as exc:
            await get_worker_config(
                response=Response(),
                platform="slack",
                team_id="T01",
                app_key="unknown",
                if_none_match=None,
                _=None,
                db=MagicMock(),
            )

    assert exc.value.status_code == 404
    assert exc.value.error_code == "WORKER-APP-001"
    connector_service.return_value.get_connector_for_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_get_worker_config_explicit_unconfigured_app_is_not_ready():
    from utils.exceptions import WorkerAppNotReadyError

    identity = MagicMock(status="unconfigured")
    identity.active_signing_secret_encrypted = None
    with (
        patch("api.routes.workers.WorkerAppIdentityService") as app_service,
        patch("api.routes.workers.ConnectorProvisioningService") as connector_service,
    ):
        app_service.return_value.get_identity = AsyncMock(return_value=identity)
        with pytest.raises(WorkerAppNotReadyError) as exc:
            await get_worker_config(
                response=Response(),
                platform="slack",
                team_id="T01",
                app_key="new-app",
                if_none_match=None,
                _=None,
                db=MagicMock(),
            )

    assert exc.value.status_code == 409
    assert exc.value.error_code == "WORKER-APP-003"
    connector_service.return_value.get_connector_for_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_get_worker_config_legacy_env_path_with_seeded_unconfigured_default():
    """The REACHABLE post-migration legacy state: e68_1315 seeds slack/default
    as status='unconfigured', so an omitted app_key must resolve through the
    legacy_unconfigured_default carve-out (env-based secrets) — not through
    the identity=None branch the other legacy tests mock, which cannot occur
    on a migrated database."""
    db = MagicMock()
    conn = MagicMock()
    conn.id = uuid4()
    conn.workspace_id = uuid4()
    conn.context_id = uuid4()
    conn.connector_type = "slack"
    conn.locale = "ja"
    conn.external_team_id = "T01"
    conn.config_version = 1
    conn.channel_ids = ["C01"]
    conn.pii_guardrail_config = {"enabled": True}
    conn.runtime_config = None
    conn.get_oauth_tokens.return_value = {
        "bot_token": "xoxb-x",
        "installing_admin_user_id": "U01",
    }
    conn.get_kmc_api_key.return_value = "kagura_writekey"
    conn.kmc_api_key_expires_at = None
    conn.get_resource_token.return_value = None
    conn.get_llm_config.return_value = {"provider": "anthropic", "model": "m", "api_key": "sk"}

    seeded_default = MagicMock()
    seeded_default.status = "unconfigured"
    seeded_default.active_signing_secret_encrypted = None

    with (
        patch("api.routes.workers.get_settings", return_value=_settings()),
        patch("api.routes.workers.ConnectorProvisioningService") as svc,
        patch("api.routes.workers.WorkerAppIdentityService") as app_svc,
    ):
        app_svc.return_value.get_identity = AsyncMock(return_value=seeded_default)
        svc.return_value.get_connector_for_dispatch = AsyncMock(return_value=conn)
        result = await get_worker_config(
            response=Response(),
            platform="slack",
            team_id="T01",
            app_key=None,
            if_none_match=None,
            _=None,
            db=db,
        )

    assert result.slack["bot_token"] == "xoxb-x"
    assert result.app_key == "default"
    svc.return_value.get_connector_for_dispatch.assert_awaited_once_with(
        connector_type="slack", external_team_id="T01", app_key="default"
    )


@pytest.mark.asyncio
async def test_worker_apps_isolates_undecryptable_secret_row():
    """One row whose ciphertext no longer decrypts (key rotation, corruption)
    must not 500 the whole bootstrap lane: the poisoned identity is served
    without secret material and every healthy identity keeps its secrets."""
    poisoned = MagicMock()
    poisoned.id = uuid4()
    poisoned.platform = "slack"
    poisoned.app_key = "poisoned"
    poisoned.status = "active"
    poisoned.config_version = 2
    poisoned.active_secret_revision = 1
    poisoned.retiring_valid_until = None
    poisoned.get_active_signing_secret.side_effect = ValueError("Failed to decrypt data")

    healthy = MagicMock()
    healthy.id = uuid4()
    healthy.platform = "slack"
    healthy.app_key = "healthy"
    healthy.status = "active"
    healthy.config_version = 3
    healthy.active_secret_revision = 2
    healthy.retiring_valid_until = None
    healthy.get_active_signing_secret.return_value = "healthy-secret"

    with patch("api.routes.workers.WorkerAppIdentityService") as app_service:
        app_service.return_value.list_identities = AsyncMock(return_value=[poisoned, healthy])
        result = await get_worker_apps(
            response=Response(), if_none_match=None, _=None, db=MagicMock()
        )

    assert result.apps[0].app_key == "poisoned"
    assert result.apps[0].active is None
    assert result.apps[0].retiring is None
    assert result.apps[1].app_key == "healthy"
    assert result.apps[1].active.signing_secret == "healthy-secret"


@pytest.mark.asyncio
async def test_worker_apps_returns_active_and_retiring_secrets_but_not_disabled_secret():
    active = MagicMock()
    active.id = uuid4()
    active.platform = "slack"
    active.app_key = "sales"
    active.status = "active"
    active.config_version = 3
    active.active_secret_revision = 3
    active.retiring_secret_revision = 2
    active.retiring_valid_until = utcnow() + timedelta(minutes=5)
    active.get_active_signing_secret.return_value = "active-secret"
    active.get_retiring_signing_secret.return_value = "old-secret"

    disabled = MagicMock()
    disabled.id = uuid4()
    disabled.platform = "slack"
    disabled.app_key = "disabled"
    disabled.status = "disabled"
    disabled.config_version = 4

    response = Response()
    with patch("api.routes.workers.WorkerAppIdentityService") as app_service:
        app_service.return_value.list_identities = AsyncMock(return_value=[active, disabled])
        result = await get_worker_apps(
            response=response, if_none_match=None, _=None, db=MagicMock()
        )
        active.get_active_signing_secret.reset_mock()
        active.get_retiring_signing_secret.reset_mock()
        not_modified = await get_worker_apps(
            response=Response(),
            if_none_match=f'"{result.revision}"',
            _=None,
            db=MagicMock(),
        )

    assert result.apps[0].active.signing_secret == "active-secret"
    assert result.apps[0].retiring.signing_secret == "old-secret"
    assert result.apps[1].active is None
    assert result.apps[1].retiring is None
    assert response.headers["etag"] == f'"{result.revision}"'
    assert not_modified.status_code == 304
    active.get_active_signing_secret.assert_not_called()
    active.get_retiring_signing_secret.assert_not_called()


# ---------------------------------------------------------------------------
# #1939: a Free-again workspace's connectors are suspended, not deleted
# ---------------------------------------------------------------------------


async def _vend_on_plan(plan: str, *, if_none_match: str | None = None):
    from models.auth import Workspace

    conn = _minimal_ready_conn("ja")
    db = MagicMock()
    db.get = AsyncMock(
        return_value=Workspace(id=conn.workspace_id, name="w", plan_name=plan, owner_user_id="u")
    )
    with (
        patch("api.routes.workers._connectors_suspended_for", new=_REAL_SUSPENSION_CHECK),
        patch("api.routes.workers.get_settings", return_value=_settings()),
        patch("api.routes.workers.ConnectorProvisioningService") as svc,
        patch("api.routes.workers.WorkerAppIdentityService") as app_svc,
    ):
        app_svc.return_value.get_identity = AsyncMock(return_value=None)
        svc.return_value.get_connector_for_dispatch = AsyncMock(return_value=conn)
        return await get_worker_config(
            response=Response(),
            platform="slack",
            team_id="T01",
            app_key=None,
            if_none_match=if_none_match,
            _=None,
            db=db,
        )


@pytest.mark.asyncio
async def test_get_worker_config_404_when_connectors_suspended_on_free():
    """The worker already skips a not-ready team; a suspended one reads the same."""
    from utils.exceptions import WorkerConnectorNotReadyError

    with pytest.raises(WorkerConnectorNotReadyError) as exc:
        await _vend_on_plan("free")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_worker_config_suspension_404_beats_304():
    """A worker holding a matching ETag from the paid days must not keep its
    cached config — the suspension is decided before the conditional GET."""
    from utils.exceptions import WorkerConnectorNotReadyError

    ready = await _vend_on_plan("promax")
    with pytest.raises(WorkerConnectorNotReadyError):
        await _vend_on_plan("free", if_none_match=f'"{ready.config_revision}"')


@pytest.mark.asyncio
async def test_get_worker_config_basic_keeps_serving_existing_connector():
    """#1551: Basic's serve-only connector seats keep dispatch running."""
    result = await _vend_on_plan("basic")
    assert result.connector_id is not None


# ---------------------------------------------------------------------------
# #1759: per-channel destinations, the capability handshake
# ---------------------------------------------------------------------------

_PIN_CONNECTOR_ID = "00000000-0000-4000-8000-000000000001"
_PIN_WORKSPACE_ID = "00000000-0000-4000-8000-000000000002"
_PIN_CONTEXT_ID = "00000000-0000-4000-8000-000000000003"

# The body and revision v0.156.0 vended for ``_pinned_conn`` (captured before
# #1759). An old worker must see no difference after the deploy: neither in
# the body, byte for byte, nor in the revision (a changed ETag would make
# every worker refetch, a changed body could fail its validation).
_PINNED_BODY = (
    '{"connector_id":"00000000-0000-4000-8000-000000000001",'
    '"workspace_id":"00000000-0000-4000-8000-000000000002",'
    '"context_id":"00000000-0000-4000-8000-000000000003","platform":"slack",'
    '"app_key":"default","config_revision":"36c94118e8367148f42c4e521ecbec07",'
    '"locale":"ja","slack":{"bot_token":"xoxb-x","team_id":"T01",'
    '"installing_admin_user_id":"U01","channel_ids":["C01","C02"]},'
    '"kmc":{"mcp_url":"https://example.com/mcp","api_key":"kagura_writekey"},'
    '"resource":null,"llm":{"provider":"p","model":"m","api_key":"sk"},'
    '"pii_guardrail_config":{"enabled":true}}'
)
_PINNED_REVISION = "36c94118e8367148f42c4e521ecbec07"


def _pinned_conn():
    from uuid import UUID

    conn = MagicMock()
    conn.id = UUID(_PIN_CONNECTOR_ID)
    conn.workspace_id = UUID(_PIN_WORKSPACE_ID)
    conn.context_id = UUID(_PIN_CONTEXT_ID)
    conn.connector_type = "slack"
    conn.locale = "ja"
    conn.external_team_id = "T01"
    conn.config_version = 7
    conn.channel_ids = ["C01", "C02"]
    conn.pii_guardrail_config = {"enabled": True}
    conn.runtime_config = None
    conn.get_oauth_tokens.return_value = {"bot_token": "xoxb-x", "installing_admin_user_id": "U01"}
    conn.get_kmc_api_key.return_value = "kagura_writekey"
    conn.kmc_api_key_expires_at = None
    conn.get_resource_token.return_value = None
    conn.get_llm_config.return_value = {"provider": "p", "model": "m", "api_key": "sk"}
    return conn


async def _vend_routed(
    routing,
    *,
    capabilities: str | None = None,
    if_none_match: str | None = None,
    conn=None,
    response: Response | None = None,
):
    with (
        patch("api.routes.workers._channel_routing_for", new=AsyncMock(return_value=routing)),
        patch(
            "api.routes.workers.get_settings",
            return_value=_settings(mcp_url="https://example.com/mcp"),
        ),
        patch("api.routes.workers.ConnectorProvisioningService") as svc,
        patch("api.routes.workers.WorkerAppIdentityService") as app_svc,
    ):
        app_svc.return_value.get_identity = AsyncMock(return_value=None)
        svc.return_value.get_connector_for_dispatch = AsyncMock(
            return_value=conn if conn is not None else _pinned_conn()
        )
        return await get_worker_config(
            response=response if response is not None else Response(),
            platform="slack",
            team_id="T01",
            app_key=None,
            capabilities=capabilities,
            if_none_match=if_none_match,
            _=None,
            db=MagicMock(),
        )


def _routing(rows=(), unrouted="default"):
    from services.connector_channel_routes import effective_routing_from_rows

    return effective_routing_from_rows(
        [
            SimpleNamespace(
                channel_id=channel_id,
                action=action,
                context_id=context_id,
                context_workspace_id=workspace_id,
                context_deleted_at=deleted_at,
                context_name=None,
                context_is_private=None,
            )
            for channel_id, action, context_id, workspace_id, deleted_at in rows
        ],
        __import__("uuid").UUID(_PIN_WORKSPACE_ID),
        unrouted,
    )


def _dump(result) -> str:
    import json

    return json.dumps(result.model_dump(mode="json"), separators=(",", ":"))


@pytest.mark.asyncio
@pytest.mark.parametrize("capabilities", [None, "", "something_else"])
async def test_vend_without_the_capability_is_byte_identical_for_an_unrouted_connector(
    capabilities,
):
    from services.connector_channel_routes import NO_ROUTING

    response = Response()
    result = await _vend_routed(NO_ROUTING, capabilities=capabilities, response=response)
    assert _dump(result) == _PINNED_BODY
    assert result.config_revision == _PINNED_REVISION
    assert response.headers["etag"] == f'"{_PINNED_REVISION}"'
    assert "channel_routes" not in result.model_dump(mode="json")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "routing",
    [
        pytest.param(("C09", "skip", None, None, None), id="a-skip-route"),
        pytest.param(("C09", "route", "ctx", "ws", None), id="a-live-route"),
        pytest.param(("C09", "route", None, None, None), id="a-missing-destination"),
    ],
)
async def test_vend_without_the_capability_refuses_a_connector_that_uses_routing(routing):
    """A worker that would write every channel into the default context is
    never served the config of a connector with a route row."""
    from uuid import UUID, uuid4

    from utils.exceptions import WorkerConnectorNotReadyError

    channel_id, action, ctx, ws, deleted = routing
    row = (
        channel_id,
        action,
        uuid4() if ctx else None,
        UUID(_PIN_WORKSPACE_ID) if ws else None,
        deleted,
    )
    with patch("api.routes.workers.logger") as log:
        with pytest.raises(WorkerConnectorNotReadyError) as exc:
            await _vend_routed(_routing([row]))
    assert exc.value.status_code == 404
    assert any(
        call.kwargs.get("reason") == "channel_routing_unsupported"
        for call in log.warning.call_args_list
    )


@pytest.mark.asyncio
async def test_vend_without_the_capability_refuses_the_switch_alone():
    from utils.exceptions import WorkerConnectorNotReadyError

    with pytest.raises(WorkerConnectorNotReadyError):
        await _vend_routed(_routing([], unrouted="skip"))


@pytest.mark.asyncio
async def test_the_routing_404_beats_a_matching_etag():
    """Decided before the conditional GET: a worker holding the pre-routing
    ETag must get the 404, not a 304 that keeps its unrouted config."""
    from utils.exceptions import WorkerConnectorNotReadyError

    with pytest.raises(WorkerConnectorNotReadyError):
        await _vend_routed(
            _routing([("C01", "skip", None, None, None)]),
            if_none_match=f'"{_PINNED_REVISION}"',
        )


@pytest.mark.asyncio
async def test_vend_with_the_capability_carries_the_effective_routes():
    from uuid import UUID, uuid4

    live, foreign = uuid4(), uuid4()
    ws = UUID(_PIN_WORKSPACE_ID)
    from datetime import datetime

    routing = _routing(
        [
            ("C05", "route", live, ws, None),
            ("C04", "route", uuid4(), ws, datetime(2026, 1, 1)),  # soft-deleted
            ("C03", "route", None, None, None),  # hard-deleted (SET NULL)
            ("C02", "route", foreign, uuid4(), None),  # another workspace
            ("C01", "skip", None, None, None),
        ],
        unrouted="skip",
    )
    result = await _vend_routed(routing, capabilities="other,channel_routes")
    body = result.model_dump(mode="json")
    assert body["channel_routes"] == [
        {"channel_id": "C01", "action": "skip"},
        {"channel_id": "C02", "action": "skip"},
        {"channel_id": "C03", "action": "skip"},
        {"channel_id": "C04", "action": "skip"},
        {"channel_id": "C05", "action": "route", "context_id": str(live)},
    ]
    assert body["unrouted_private_channels"] == "skip"
    assert str(foreign) not in _dump(result)
    # Everything an old worker reads is still there, unchanged.
    pinned = __import__("json").loads(_PINNED_BODY)
    for key in pinned:
        if key != "config_revision":
            assert body[key] == pinned[key]


@pytest.mark.asyncio
async def test_vend_with_the_capability_carries_empty_routing_when_unused():
    from services.connector_channel_routes import NO_ROUTING

    result = await _vend_routed(NO_ROUTING, capabilities="channel_routes")
    body = result.model_dump(mode="json")
    assert body["channel_routes"] == []
    assert body["unrouted_private_channels"] == "default"
    # The capability is part of the revision input.
    assert result.config_revision != _PINNED_REVISION


@pytest.mark.asyncio
async def test_a_deleted_destination_changes_the_revision_without_a_version_bump():
    from datetime import datetime
    from uuid import UUID, uuid4

    ctx = uuid4()
    ws = UUID(_PIN_WORKSPACE_ID)
    live = await _vend_routed(
        _routing([("C01", "route", ctx, ws, None)]), capabilities="channel_routes"
    )
    again = await _vend_routed(
        _routing([("C01", "route", ctx, ws, None)]), capabilities="channel_routes"
    )
    soft = await _vend_routed(
        _routing([("C01", "route", ctx, ws, datetime(2026, 1, 1))]), capabilities="channel_routes"
    )
    hard = await _vend_routed(
        _routing([("C01", "route", None, None, None)]), capabilities="channel_routes"
    )
    assert live.config_revision == again.config_revision
    assert soft.config_revision != live.config_revision
    assert hard.config_revision != live.config_revision
    # A matching ETag after the deletion is no longer a 304.
    response = await _vend_routed(
        _routing([("C01", "route", ctx, ws, datetime(2026, 1, 1))]),
        capabilities="channel_routes",
        if_none_match=f'"{live.config_revision}"',
    )
    assert not isinstance(response, Response)
    # ...and an unchanged one is.
    cached = await _vend_routed(
        _routing([("C01", "route", ctx, ws, None)]),
        capabilities="channel_routes",
        if_none_match=f'"{live.config_revision}"',
    )
    assert isinstance(cached, Response)
    assert cached.status_code == 304


# ---------------------------------------------------------------------------
# A soft-deleted default context is not ready
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_default_context_that_is_not_live_is_not_ready_even_on_a_matching_etag():
    """A soft delete keeps the id and bumps no config_version: decided before
    the conditional GET, with its own log reason."""
    from services.connector_channel_routes import NO_ROUTING
    from utils.exceptions import WorkerConnectorNotReadyError

    for if_none_match in (None, f'"{_PINNED_REVISION}"'):
        with (
            patch("api.routes.workers._default_context_live", new=AsyncMock(return_value=False)),
            patch("api.routes.workers.logger") as log,
            pytest.raises(WorkerConnectorNotReadyError) as exc,
        ):
            await _vend_routed(NO_ROUTING, if_none_match=if_none_match)
        assert exc.value.status_code == 404
        assert any(
            call.kwargs.get("reason") == "default_context_unavailable"
            for call in log.warning.call_args_list
        )


# The real lookup, taken at import (the autouse fixture patches the name).
_REAL_DEFAULT_CONTEXT_LIVE = workers_module._default_context_live


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "live"),
    [
        pytest.param("live", True, id="live"),
        pytest.param("soft-deleted", False, id="soft-deleted"),
        pytest.param("other-workspace", False, id="other-workspace"),
        pytest.param(None, False, id="no-row"),
    ],
)
async def test_default_context_live_reads_existence_deletion_and_workspace(row, live):
    from datetime import datetime
    from uuid import uuid4

    conn = _pinned_conn()
    values = {
        "live": SimpleNamespace(workspace_id=conn.workspace_id, deleted_at=None),
        "soft-deleted": SimpleNamespace(
            workspace_id=conn.workspace_id, deleted_at=datetime(2026, 1, 1)
        ),
        "other-workspace": SimpleNamespace(workspace_id=uuid4(), deleted_at=None),
        None: None,
    }
    result = MagicMock()
    result.first.return_value = values[row]
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    assert await _REAL_DEFAULT_CONTEXT_LIVE(db, conn) is live
