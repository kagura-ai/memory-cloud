"""ai-worker service endpoints (Spec 2026-06-02, Plan 3).

The kagura-chat-bridge is a Kagura-operated shared multi-tenant worker
(Model B). It dispatches inbound platform events (e.g. Slack) by team id, then
fetches the per-connector config from here — replacing the static
``connector.json`` file. Authenticated by a dedicated worker service token
(NOT a user session or workspace API key), and intended to be reachable only
over the internal network (not exposed publicly via Caddy).

The response intentionally carries secrets (Slack bot token, BYO LLM key, the
workspace-scoped KMC write key) decrypted at read time — callers MUST treat the
payload as sensitive and never log it.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, SerializerFunctionWrapHandler, model_serializer
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.worker_runtime import WorkerLocale, WorkerRuntimeConfig, normalize_worker_locale
from services.connector_provisioning import ConnectorProvisioningService
from services.worker_app_identity import (
    WorkerAppIdentityService,
    identity_collection_revision,
    identity_revision,
    opaque_revision,
)
from utils.exceptions import (
    WorkerAppDisabledError,
    WorkerAppNotFoundError,
    WorkerAppNotReadyError,
    WorkerConnectorNotReadyError,
)
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/workers", tags=["workers"])


async def verify_worker_token(authorization: str | None = Header(None)) -> None:
    """Authenticate the ai-worker by its shared service token (RFC 6750 Bearer).

    Fail-closed: an unset ``WORKER_SERVICE_TOKEN`` disables the endpoint (503),
    so a misconfigured deployment never serves connector secrets unauthenticated.
    """
    expected = get_settings().worker_service_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Worker config endpoint is not configured",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Worker service token required",
        )
    token = authorization[len("Bearer ") :]
    if not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid worker service token",
        )


class WorkerConnectorConfig(TZAwareBaseModel):
    """Per-connector config handed to the ai-worker. Contains secrets."""

    connector_id: UUID
    workspace_id: UUID
    context_id: UUID
    platform: str
    app_key: str
    config_revision: str
    # #1377: typed to the worker Locale contract so the OpenAPI schema carries
    # the enum and the two repos cannot drift silently. The vend site
    # normalizes legacy stored values before construction.
    locale: WorkerLocale | None = None
    slack: dict[str, Any]
    kmc: dict[str, Any]
    # #895: resource-ingest credentials for worker #91 Option A. NULL on legacy
    # connectors provisioned before the resource-token-encrypted column existed —
    # the worker falls back to the kmc/remember write path when absent.
    resource: dict[str, Any] | None = None
    llm: dict[str, Any] | None = None
    pii_guardrail_config: dict[str, Any] | None = None
    runtime: WorkerRuntimeConfig | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_runtime(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> dict[str, Any]:
        """Keep legacy nullable fields while omitting only the additive block."""
        payload: dict[str, Any] = handler(self)
        if self.runtime is None:
            payload.pop("runtime", None)
        return payload


class WorkerSigningSecret(TZAwareBaseModel):
    """One revision of signing-secret material for an internal worker."""

    revision: int
    signing_secret: str
    valid_until: datetime | None = None


class WorkerAppBootstrapItem(TZAwareBaseModel):
    """One app identity. Secrets are present only while the app is active."""

    app_key: str
    platform: str
    status: str
    revision: str
    active: WorkerSigningSecret | None = None
    retiring: WorkerSigningSecret | None = None


class WorkerAppBootstrapResponse(BaseModel):
    revision: str
    apps: list[WorkerAppBootstrapItem]


def _etag(revision: str) -> str:
    return f'"{revision}"'


# The keys the bridge's ``LLMConfig`` requires to construct a client. Note the
# asymmetry with ``ConnectorProvisioningService._validate_llm_config``, which
# deliberately does NOT require ``api_key`` (provider-dependent — local ollama
# has none): a bundle can be valid to *store* and still be un-vendable. This
# gate follows the consumer, not the writer.
_VENDABLE_LLM_KEYS = ("provider", "model", "api_key")


def _is_vendable_llm_config(config: Any) -> bool:
    """Whether an LLM bundle is complete enough for the worker to use (#1447).

    Args:
        config: The decrypted ``llm_config`` document, or ``None`` when unset.
            Typed ``Any`` because the column stores arbitrary decrypted JSON —
            a drifted row may hold a non-dict, which must be rejected here
            rather than 500 in response validation.

    Returns:
        True when every key in :data:`_VENDABLE_LLM_KEYS` is a non-blank
        string. Extra keys pass through opaquely (provider-specific options).
    """
    if not isinstance(config, dict):
        return False
    return all(
        isinstance(config.get(key), str) and config[key].strip() for key in _VENDABLE_LLM_KEYS
    )


def _vend_locale(connector: Any) -> WorkerLocale | None:
    """Best-effort normalization of a stored locale to ``WORKER_LOCALES``.

    #1377 rolling compat for pre-fix rows: the read boundary degrades a
    non-conforming value to ``None`` (worker default) instead of failing the
    tenant closed — same fail-open principle as
    ``WorkerRuntimeConfig.from_stored``.
    """
    try:
        return normalize_worker_locale(connector.locale)
    except ValueError:
        logger.warning(
            "worker_config_locale_nonconforming",
            connector_id=str(connector.id),
            locale=str(connector.locale)[:32],
        )
        return None


@router.get("/apps", response_model=WorkerAppBootstrapResponse)
async def get_worker_apps(
    response: Response,
    if_none_match: str | None = Header(None, alias="If-None-Match"),
    _: None = Depends(verify_worker_token),
    db: AsyncSession = Depends(get_db),
) -> WorkerAppBootstrapResponse | Response:
    """Bootstrap app identities and signing secrets over the internal lane.

    This service token grants no memory authority. Disabled/unconfigured apps
    remain in the list without secret material so a polling worker can evict
    stale verification state deterministically.
    """
    identities = await WorkerAppIdentityService(db).list_identities()
    revision = identity_collection_revision(identities)
    etag = _etag(revision)
    headers = {"ETag": etag, "Cache-Control": "no-store"}
    if if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    response.headers.update(headers)

    from utils.datetime import utcnow

    now = utcnow()
    apps: list[WorkerAppBootstrapItem] = []
    for identity in identities:
        active = None
        retiring = None
        if identity.status == "active":
            try:
                active_secret = identity.get_active_signing_secret()
                if active_secret and identity.active_secret_revision is not None:
                    active = WorkerSigningSecret(
                        revision=identity.active_secret_revision,
                        signing_secret=active_secret,
                    )
                if identity.retiring_valid_until and identity.retiring_valid_until > now:
                    retiring_secret = identity.get_retiring_signing_secret()
                    if retiring_secret and identity.retiring_secret_revision is not None:
                        retiring = WorkerSigningSecret(
                            revision=identity.retiring_secret_revision,
                            signing_secret=retiring_secret,
                            valid_until=identity.retiring_valid_until,
                        )
            except ValueError:
                # One undecryptable row (encryption-key rotation, corrupted
                # ciphertext) must not 500 the whole fleet's bootstrap lane.
                # Serve the item without secret material — the documented
                # eviction semantics for a secretless entry — and log loudly
                # (no secret material in this log line).
                active = None
                retiring = None
                logger.warning(
                    "worker_app_bootstrap_undecryptable_secret",
                    platform=identity.platform,
                    app_key=identity.app_key,
                    active_secret_revision=identity.active_secret_revision,
                )
        apps.append(
            WorkerAppBootstrapItem(
                app_key=identity.app_key,
                platform=identity.platform,
                status=identity.status,
                revision=identity_revision(identity),
                active=active,
                retiring=retiring,
            )
        )
    return WorkerAppBootstrapResponse(revision=revision, apps=apps)


@router.get(
    "/config",
    response_model=WorkerConnectorConfig,
)
async def get_worker_config(
    response: Response,
    # Only Slack is implemented end-to-end (the response carries a Slack-specific
    # ``slack`` block). Widen to discord/teams when those connectors ship.
    platform: Literal["slack"],
    team_id: str = Query(..., max_length=255),
    app_key: str | None = Query(
        None, min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$"
    ),
    if_none_match: str | None = Header(None, alias="If-None-Match"),
    _: None = Depends(verify_worker_token),
    db: AsyncSession = Depends(get_db),
) -> WorkerConnectorConfig | Response:
    """Return the connector config for a platform team (worker dispatch).

    404 when no connector serves the team, the connector has no write-target
    context yet (registration incomplete), it has no KMC write key, or its
    ``llm_config`` cannot vend (#1447) — the worker treats all of them as
    not-ready and skips the team rather than failing the dispatch.
    """
    selected_app_key = app_key or "default"
    app_config_version = 0
    identity = await WorkerAppIdentityService(db).get_identity(platform, selected_app_key)
    if identity is None:
        if app_key is not None:
            raise WorkerAppNotFoundError(app_key)
    else:
        if identity.status == "disabled":
            raise WorkerAppDisabledError(selected_app_key)
        # An unconfigured default identity is the deliberate Phase-3a migration
        # state: legacy callers that omit app_key still use the worker env
        # signing secret. Explicit app-qualified callers never get that bypass.
        legacy_unconfigured_default = (
            app_key is None and selected_app_key == "default" and identity.status == "unconfigured"
        )
        if not legacy_unconfigured_default and (
            identity.status != "active" or not identity.active_signing_secret_encrypted
        ):
            raise WorkerAppNotReadyError(selected_app_key)
        app_config_version = identity.config_version

    connector = await ConnectorProvisioningService(db).get_connector_for_dispatch(
        connector_type=platform,
        external_team_id=team_id,
        app_key=selected_app_key,
    )
    if connector is None or connector.context_id is None:
        raise WorkerConnectorNotReadyError()

    # #1447: readiness is decided BEFORE the conditional-GET short-circuit. A 304
    # asserts "your cached representation is still valid" — but when the resource
    # would now be a 404, the cached 200 body is precisely what has to be thrown
    # away. ``config_revision`` is derived from ids + config_version only, so any
    # readiness change that does not bump config_version (a deployment flipping
    # ``enable_managed_connectors``, a rotated encryption key) would otherwise
    # keep a poisoned config alive behind a matching ETag indefinitely. The cost
    # is decrypting on the 304 path too, which is negligible beside the connector
    # lookup already performed above.
    #
    # Because the decrypt now also runs on the cache-hit path, a corrupt or
    # unrotatable ciphertext must not become a 500 there (review round 2): a
    # credential we cannot read is a credential the connector does not have.
    try:
        kmc_api_key = connector.get_kmc_api_key()
    except Exception as exc:
        logger.warning(
            "worker_config_kmc_key_undecryptable",
            connector_id=str(connector.id),
            error_type=type(exc).__name__,
        )
        raise WorkerConnectorNotReadyError() from None
    if not kmc_api_key:
        raise WorkerConnectorNotReadyError()

    # #1447: an un-vendable LLM bundle is a not-ready connector, not a ready one
    # with a hole in it. Vending 200 here poisons the tenant: the worker rejects
    # the config, the Slack webhook still answers 200, and events are dropped
    # with no retry and no error anywhere — a 2026-07-21..27 production outage
    # ran 6 days that way. 404 is the state the worker already handles (skip).
    #
    # Managed SaaS (#1426) is the documented exception: there the shared bridge
    # supplies the pre-compile LLM, which is exactly what
    # ``enable_managed_connectors`` already means ("stops flagging a missing
    # per-connector LLM"). This aligns the vend gate with the flag the admin UI
    # has been honouring all along.
    #
    # The carve-out covers an ABSENT bundle only. A present-but-broken one is
    # not-ready in every mode: the worker would try to use it and fail closed,
    # and silently falling back to the shared LLM would hide a half-finished
    # BYO setup. (It also keeps a drifted non-dict document from reaching
    # response validation, where it raises a 500 instead of a 404.)
    try:
        llm_config = connector.get_llm_config()
    except Exception as exc:
        # A stored bundle that cannot be decrypted or parsed (rotated Fernet key,
        # corrupted ciphertext) is un-vendable in exactly the same sense — it must
        # take the 404 path the worker already handles, not surface as a 500 the
        # worker has no rule for. Same fail-soft principle as the undecryptable
        # signing secret in ``get_worker_apps`` above.
        #
        # The catch is deliberately broad: a readiness boundary that lets an
        # unanticipated exception through takes the tenant down, which is the
        # failure mode this whole change exists to remove. ``error_type`` is
        # logged so a masked programming error (a renamed column, a changed
        # return type) is still visible rather than reading as a benign
        # not-ready connector.
        logger.warning(
            "worker_config_llm_undecryptable",
            connector_id=str(connector.id),
            error_type=type(exc).__name__,
        )
        raise WorkerConnectorNotReadyError() from None

    managed = get_settings().enable_managed_connectors
    if not _is_vendable_llm_config(llm_config) and not (managed and llm_config is None):
        # Fires on every poll while the connector stays broken, which is the
        # intended cadence for a state an operator must act on (and is the same
        # cadence as ``connector_kmc_key_expired`` below). #1449 adds the durable
        # admin-UI surface so this log is not the only place it shows up.
        logger.warning(
            "worker_config_llm_not_vendable",
            connector_id=str(connector.id),
            # Key names only — never the bundle, which holds the API key.
            present_keys=sorted(llm_config) if isinstance(llm_config, dict) else [],
        )
        raise WorkerConnectorNotReadyError()

    config_revision = opaque_revision(
        connector.id,
        connector.config_version,
        selected_app_key,
        app_config_version,
    )
    etag = _etag(config_revision)
    headers = {"ETag": etag, "Cache-Control": "no-store"}
    if if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    response.headers.update(headers)

    from utils.datetime import utcnow

    if connector.kmc_api_key_expires_at and connector.kmc_api_key_expires_at < utcnow():
        logger.warning(
            "connector_kmc_key_expired",
            connector_id=str(connector.id),
            expired_at=connector.kmc_api_key_expires_at.isoformat(),
        )

    oauth = connector.get_oauth_tokens() or {}
    slack = {
        "bot_token": oauth.get("bot_token"),
        "team_id": connector.external_team_id,
        "installing_admin_user_id": oauth.get("installing_admin_user_id"),
        "channel_ids": connector.channel_ids or [],
    }

    # #895: resource-ingest credentials for worker #91 Option A. Only emitted
    # when the connector has a stored resource token (legacy rows lack it →
    # worker uses the kmc/remember fallback). Resolve the resource slug from
    # resource_pk for the POST /api/v1/resources/{resource_id}/events path.
    resource_block = None
    resource_token = connector.get_resource_token()
    if resource_token:
        from sqlalchemy import select

        from models.resource import Resource

        slug_result = await db.execute(
            select(Resource.resource_id).where(
                Resource.id == connector.resource_pk,
                # Defense-in-depth: workspace_id is denormalized and must match
                # the connector's; the predicate prevents returning a slug from
                # another workspace if the FK ever drifts.
                Resource.workspace_id == connector.workspace_id,
            )
        )
        resource_slug = slug_result.scalar_one_or_none()
        if resource_slug:
            resource_block = {"id": resource_slug, "api_key": resource_token}

    return WorkerConnectorConfig(
        connector_id=connector.id,
        workspace_id=connector.workspace_id,
        context_id=connector.context_id,
        platform=connector.connector_type,
        app_key=selected_app_key,
        config_revision=config_revision,
        locale=_vend_locale(connector),
        slack=slack,
        kmc={"mcp_url": get_settings().kmc_mcp_url, "api_key": kmc_api_key},
        resource=resource_block,
        llm=llm_config,
        pii_guardrail_config=connector.pii_guardrail_config,
        # Lenient rehydrate (#1350 review): a stored document drifted across
        # releases must degrade to "no runtime block, worker defaults" — a
        # strict-validation 500 here is a full connector outage (the worker
        # cannot fetch its token/KMC key either).
        runtime=WorkerRuntimeConfig.from_stored(connector.runtime_config),
    )
