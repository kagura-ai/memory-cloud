"""Tests for the ai-worker config endpoint (Spec 2026-06-02, Plan 3)."""

from datetime import timedelta
from json import JSONDecodeError
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from cryptography.fernet import InvalidToken
from fastapi import Response

from api.routes.workers import (
    WorkerConnectorConfig,
    get_worker_apps,
    get_worker_config,
    verify_worker_token,
)
from utils.datetime import utcnow

# #1447: the minimum bundle the bridge's ``LLMConfig`` accepts. A connector
# missing any of these cannot vend, so the readiness gate rejects it.
_VENDABLE_LLM = {"provider": "anthropic", "model": "m", "api_key": "sk"}


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
        assert await verify_worker_token("Bearer wt-secret") is None


@pytest.mark.asyncio
async def test_verify_worker_token_rejects_wrong_token():
    from fastapi import HTTPException

    with patch("api.routes.workers.get_settings", return_value=_settings()):
        with pytest.raises(HTTPException) as exc:
            await verify_worker_token("Bearer nope")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_worker_token_503_when_unconfigured():
    from fastapi import HTTPException

    with patch("api.routes.workers.get_settings", return_value=_settings(token="")):
        with pytest.raises(HTTPException) as exc:
            await verify_worker_token("Bearer anything")
    assert exc.value.status_code == 503


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
