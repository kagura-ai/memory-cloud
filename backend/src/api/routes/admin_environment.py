"""Admin read of the effective environment settings (Issue #2837).

``GET /api/v1/admin/environment`` — every key of the Environment console's
categories with the value the answering process uses, and the embedding
models the deployment offers. Behind the system-admin gate in every
``SYSTEM_ADMIN_MODE`` (``AdminUser``: a system admin in ``session`` mode, an
operator principal in ``service`` mode; ``off`` is 404). Every caller of the
route gets every category, the admin-only ones (``hosted``, ``signup``,
``rewards``, ``operations``) included.

Which values are answered is an allow-list: ``ANSWERED_KEYS``. A category key
that is not on it, or that is in ``get_sensitive_keys()``, answers the
constant ``REDACTED_VALUE`` and is not resolved at all. Adding a key to the
categories therefore shows it with the marker until it is added here on
purpose (a test pins ``ANSWERED_KEYS`` to today's category keys).

The route reads the process's settings only: no database session, no stored
key, no outbound call.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import ConfigDict

from api.routes import config as config_routes
from auth.dependencies import AdminUser
from config.constants import EMBEDDING_MODEL_REGISTRY
from config.embedding_policy import allowed_embedding_models
from config.settings import Settings, get_settings
from models.api_base import TZAwareBaseModel
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# The category keys whose effective value this route answers.
ANSWERED_KEYS: frozenset[str] = frozenset(
    {
        # embedding
        "EMBEDDING_PROVIDER",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSIONS",
        # search
        "ENABLE_RERANKING",
        "HYBRID_FUSION",
        # system
        "ENVIRONMENT",
        "LOG_LEVEL",
        "CORS_ORIGINS",
        "ENABLE_NEURAL_MEMORY",
        "TRACK_CO_ACTIVATION",
        "ENABLE_DECAY",
        "ENABLE_TRUST_MODULATION",
        # hosted
        "ENABLE_BYOK",
        "RESOLVE_STORED_BYOK_KEYS",
        "ENABLE_COST_DISPLAY",
        "ENABLE_PLAN_PAGE",
        "MANAGED_LLM_PROVIDER",
        "MANAGED_LLM_MODEL",
        "DEFAULT_USE_RERANK",
        "DEFAULT_RERANKER_PROVIDER",
        "DEFAULT_RERANKER_MODEL",
        "DEFAULT_CONTENT_CHUNKS_ENABLED",
        "RERANK_BASE_URL",
        "RERANK_MODEL",
        # signup (#2954)
        "ALLOW_REGISTRATION",
        "ENABLE_EMAIL_SIGNUP",
        "ENABLE_PROVIDER_LINK_CONFIRMATION",
        "ENABLE_BETA_INVITES",
        "BETA_INVITE_QUOTA_PER_USER",
        # rewards (#2954)
        "ENABLE_REFERRALS",
        "REFERRAL_REFEREE_REWARD_MEMORIES",
        "REFERRAL_REFERRER_REWARD_MEMORIES",
        "REFERRAL_MAX_GRANTS_PER_REFERRER",
        "REFERRAL_REDEEM_WINDOW_HOURS",
        # operations (#2954)
        "MCP_NOTICES_ENABLED",
        "REQUIRE_DUAL_CONTROL_FORCE_TRANSFER",
        "BM25_DRIFT_CRON_ENABLED",
        "BM25_REVEAL_RATE_LIMIT_PER_HOUR",
        "OPERATIONS_PROBE_INTERVAL_SECONDS",
        "FILE_SCAN_ENABLED",
        "SLEEP_LLM_FORCE_ENV",
    }
)

# What a category key outside ``ANSWERED_KEYS`` (or a sensitive one) answers
# as its value, whatever the value is.
REDACTED_VALUE = "***MASKED***"


class EnvironmentConfigItem(TZAwareBaseModel):
    """One environment-backed setting of the answering process."""

    model_config = ConfigDict(extra="forbid")

    key: str
    category: str
    value: bool | int | float | str | None
    type: str | None
    description: str | None
    requires_restart: bool | None


class OfferedEmbeddingModel(TZAwareBaseModel):
    """An embedding model the deployment offers."""

    model_config = ConfigDict(extra="forbid")

    name: str
    dimensions: int
    provider: str


class EnvironmentEmbeddingModels(TZAwareBaseModel):
    """The deployment's default embedding model and the ones it offers."""

    model_config = ConfigDict(extra="forbid")

    default_model: str
    offered: list[OfferedEmbeddingModel]


class EnvironmentResponse(TZAwareBaseModel):
    """The effective environment settings of the answering process."""

    model_config = ConfigDict(extra="forbid")

    configs: list[EnvironmentConfigItem]
    embedding_models: EnvironmentEmbeddingModels


def _answered_value(key: str, settings: Settings, sensitive_keys: set[str]) -> Any:
    """The value to answer for ``key``: its effective value, or the marker.

    A key outside ``ANSWERED_KEYS`` or inside ``sensitive_keys`` is not
    resolved. A string value has the credentials of a URL masked, whatever
    the key.
    """
    if key not in ANSWERED_KEYS or key in sensitive_keys:
        return REDACTED_VALUE
    value = config_routes.get_effective_value(key, settings)
    if isinstance(value, str):
        return config_routes.mask_url_credentials(value)
    return value


@router.get(
    "/environment",
    response_model=EnvironmentResponse,
    summary="The effective environment settings of the answering process",
)
async def get_environment(admin: AdminUser) -> EnvironmentResponse:
    """The environment-backed settings as the process that answers uses them.

    - `configs`: every key of the Environment console's categories
      (`embedding`, `search`, `system`, `hosted`, `signup`, `rewards`,
      `operations`), in that order, with its
      effective `value` and the `type`, `description` and `requires_restart`
      of its schema entry (null for a key without one). Every key is set by
      the environment and applied on restart. Credentials in a URL value are
      masked (`https://***@host`). A key whose value is not answered carries
      `***MASKED***` as its `value`.
    - `embedding_models`: `default_model` and the models the deployment
      `offered`, each with `name`, `dimensions` and `provider`.

    Processes of one deployment can run with different environments: the
    answer describes the process that served this request.
    """
    settings = get_settings()
    schema = config_routes.get_config_schema()
    sensitive_keys = config_routes.get_sensitive_keys()

    configs: list[EnvironmentConfigItem] = []
    for category, keys in config_routes.get_config_categories().items():
        for key in keys:
            entry = schema.get(key)
            configs.append(
                EnvironmentConfigItem(
                    key=key,
                    category=category,
                    value=_answered_value(key, settings, sensitive_keys),
                    type=entry.type if entry is not None else None,
                    description=entry.description if entry is not None else None,
                    requires_restart=entry.requires_restart if entry is not None else None,
                )
            )

    offered_names = set(allowed_embedding_models(settings.embedding_model_allowlist))
    embedding_models = EnvironmentEmbeddingModels(
        default_model=settings.embedding_model,
        offered=[
            OfferedEmbeddingModel(name=name, dimensions=dimensions, provider=provider)
            for name, (dimensions, provider) in EMBEDDING_MODEL_REGISTRY.items()
            if name in offered_names
        ],
    )

    logger.info("admin_environment_read", admin_user=admin["user_id"], count=len(configs))
    return EnvironmentResponse(configs=configs, embedding_models=embedding_models)
