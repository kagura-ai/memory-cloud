"""Application settings using Pydantic Settings.

Environment variables are loaded from .env file.
Use ENV_FILE environment variable to specify which file to load.
Database URLs are managed directly via os.getenv() in config/database.py.
"""

import os
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from utils.media_types import MEDIA_TYPE_RE, normalize_media_type

# Issue #1470: the total memory quota one inviter's referral chain may mint.
#
# Derived from the FREE -> BASIC memory gap: ``PLAN_BASIC.memory_limit (10000) -
# PLAN_FREE.memory_limit (1000)``. If a fully-used referral chain could close
# that gap, the referral program would BE the paid tier, for free.
#
# Hardcoded rather than imported from ``config.plan_tiers`` because that module
# calls ``get_settings()`` at import time (``_apply_settings_overrides``), so
# importing it here would be circular. The coupling is instead pinned by
# ``tests/api/test_referrals.py::test_payout_budget_matches_the_free_to_basic_gap``,
# which fails if the tier values move and this constant does not.
REFERRAL_TOTAL_PAYOUT_BUDGET_MEMORIES = 9000


def _validate_handoff_base_url(value: str) -> str:
    """Validate/normalize the optional billing handoff base URL (#1118).

    Empty (default) → ``""`` (the convenience is inert). When set, the value must
    be an ``https`` origin (``http`` allowed only for localhost) with NO path
    component: the redirect is materialized as ``{base}/enter?t={token}``, so a
    non-https scheme would leak the short-lived token over plaintext (server /
    proxy logs, Referer) and a path component would break the frozen ``/enter``
    cross-repo contract. Raises ``ValueError`` (loud boot-time failure) on a
    malformed base rather than silently emitting a broken redirect URL.
    """
    v = (value or "").strip()
    if not v:
        return ""
    parsed = urlparse(v)
    host_is_local = parsed.hostname in {"localhost", "127.0.0.1"}
    if not (parsed.scheme == "https" or (parsed.scheme == "http" and host_is_local)):
        raise ValueError(
            "payment_public_base_url must use https:// (http:// allowed only for localhost)"
        )
    if parsed.path not in ("", "/"):
        raise ValueError("payment_public_base_url must be an origin with no path component")
    return v


class Settings(BaseSettings):
    """Application settings."""

    model_config = SettingsConfigDict(
        env_file=os.getenv("ENV_FILE", ".env.dev"),  # Default: .env.dev
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_prefix="",  # No prefix
    )

    # OAuth2 - PKCE enforcement (Issue #513)
    # When True, register the Authlib CodeChallenge extension with
    # ``required=True`` so token_endpoint_auth_method="none" (public) clients
    # cannot complete authorization without RFC 7636 PKCE — closes the gap
    # introduced when Issue #157 added `none` auth without registering the
    # CodeChallenge extension. Set to False only as an emergency rollback if a
    # known-good client breaks; the default (True) matches the metadata this
    # server already advertises (``code_challenge_methods_supported: ["S256"]``).
    oauth_pkce_required: bool = Field(
        default=True,
        description="Enforce PKCE (RFC 7636) at /token for public clients",
    )

    # OAuth2 - Device Authorization Grant (RFC 8628, Issue #536)
    oauth_device_code_expires_in: int = Field(
        default=600,
        description="Device code lifetime in seconds (default: 10 min per RFC 8628)",
    )
    oauth_device_polling_interval: int = Field(
        default=5,
        description="Minimum polling interval in seconds for device code grant",
    )

    # OAuth2 - Google
    google_client_id: str = Field(default="", description="Google OAuth2 Client ID")
    google_client_secret: str = Field(default="", description="Google OAuth2 Client Secret")
    google_redirect_uri: str = Field(
        default="http://localhost:8080/auth/google/callback",
        description="OAuth2 Redirect URI",
    )

    # OAuth2 - GitHub (optional)
    github_client_id: str | None = Field(default=None, description="GitHub OAuth2 Client ID")
    github_client_secret: str | None = Field(
        default=None, description="GitHub OAuth2 Client Secret"
    )

    # OAuth2 - Azure (optional)
    azure_client_id: str | None = Field(default=None, description="Azure OAuth2 Client ID")
    azure_client_secret: str | None = Field(default=None, description="Azure OAuth2 Client Secret")
    azure_tenant_id: str | None = Field(default=None, description="Azure Tenant ID")

    # Security
    api_key_secret: str = Field(
        default="dev-secret-change-in-production",
        description="API Key encryption secret (32+ bytes)",
    )
    # Issue #947: throttle window (seconds) for api_keys.last_used_at writes.
    # last_used_at is updated at most once per window per key, so a hot MCP
    # client (one auth per tool call) does not trigger a row UPDATE+commit on
    # every request. Minute-level precision is plenty for the "Last used" UI.
    api_key_last_used_throttle_seconds: int = Field(
        default=60,
        description="Min seconds between api_keys.last_used_at writes per key (Issue #947)",
    )
    # Issue #1257: same pattern for contexts.last_used_at — memory operations
    # (remember/recall/reference) mark the context as used so the list_contexts
    # recency sort means something, but a context row is hotter than an API key
    # row (every memory op in the workspace hits it), so the write is throttled
    # to at most one per context per window.
    context_last_used_throttle_seconds: int = Field(
        default=60,
        description="Min seconds between contexts.last_used_at writes per context (Issue #1257)",
    )
    # Issue #1274 (RFC-0002 P0-1): same pattern for agents.last_seen_at — the
    # correlation/verify paths (P0-2/P0-4) mark the agent as seen, but a hot
    # agent authenticates on every tool call, so the write is throttled to at
    # most one per agent per window.
    agent_last_seen_throttle_seconds: int = Field(
        default=60,
        description="Min seconds between agents.last_seen_at writes per agent (Issue #1274)",
    )
    # Issue #1274: registry rows are cheap control-plane metadata, so the cap
    # is an anti-abuse guard (unbounded enumeration surface), not a plan knob.
    # Applies to all plans; raise per deployment if a tenant legitimately runs
    # a larger agent fleet.
    max_agents_per_workspace: int = Field(
        default=100,
        description="Max registered agents per workspace (Issue #1274)",
    )
    # ai-worker (kagura-chat-bridge) service auth. The worker presents this
    # as a Bearer token to GET /api/v1/workers/config (Spec 2026-06-02). Empty
    # disables the worker config endpoint (fail-closed). Use: openssl rand -hex 32.
    #
    # SECURITY NOTE: Settings are read once at process startup and cached as a
    # module-level singleton (see get_settings()). Rotating WORKER_SERVICE_TOKEN
    # in the environment requires a process restart to take effect — the in-memory
    # cached value is not reloaded automatically. This applies to all secrets in
    # this class. Ensure your secret-rotation runbook includes a rolling restart.
    worker_service_token: str = Field(
        default="",
        description="Shared bearer token authenticating the ai-worker to /api/v1/workers/* (RFC 6750)",
    )
    # Issue #954: service-to-service auth for the external billing service
    # pushing entitlement changes to PUT /internal/...
    # Empty disables the internal billing endpoints (fail-closed, 503). Distinct
    # from worker_service_token so the two services can be rotated independently.
    # Use: openssl rand -hex 32. See the rotation note on worker_service_token.
    billing_service_token: str = Field(
        default="",
        description="Shared bearer token authenticating the billing service to /internal/* (RFC 6750)",
    )
    # Issue #1093: Ed25519 signing material for owner billing handoff tokens
    # (POST /api/v1/billing/handoff). memory-cloud holds the PRIVATE key; the
    # billing service verifies with the matching public key (kid-based rotation).
    # Empty key OR empty kid disables the endpoint (fail-closed, 503 BILLING-002).
    # Generate: openssl genpkey -algorithm ed25519 -out handoff.pem
    billing_handoff_signing_key: str = Field(
        default="",
        description=(
            "Ed25519 private key (PEM / PKCS8) signing billing handoff tokens. "
            "Empty disables POST /api/v1/billing/handoff (fail-closed, 503)."
        ),
    )
    billing_handoff_key_id: str = Field(
        default="",
        description=(
            "kid for the active billing handoff signing key (verifier-side key "
            "rotation). Empty disables the endpoint (fail-closed, 503)."
        ),
    )
    billing_handoff_issuer: str = Field(
        default="kagura-memory-cloud",
        description=(
            "iss claim stamped on billing handoff tokens. A FROZEN cross-repo "
            "JWT contract value verified by the external billing service — NOT a "
            "repo or service name. Changing it is a coordinated breaking change "
            "(the verifier must change in lockstep), never a local rename."
        ),
    )
    billing_handoff_audience: str = Field(
        default="kagura-billing",
        description=(
            "aud claim — the billing audience served by the external billing "
            "service that verifies handoff tokens ('billing' is the function; the "
            "external service provides it). A FROZEN cross-repo JWT contract value, "
            "NOT a repo name; changing it is a coordinated breaking change (the "
            "verifier must change in lockstep), never a local rename."
        ),
    )
    billing_handoff_ttl_seconds: int = Field(
        default=120,
        ge=1,
        le=900,
        description="Billing handoff token lifetime in seconds (short-lived; <=15min).",
    )
    billing_handoff_rate_limit_per_minute: int = Field(
        default=10,
        description=(
            "Per-(owner, workspace) mint rate limit for POST /api/v1/billing/handoff "
            "(#1104). Abuse bound, not a per-plan quota. <=0 disables minting "
            "(fail-safe). Fail-open on a Redis outage."
        ),
    )
    # #1118: optional public base URL (origin) of the external billing service's
    # handoff entry. When set, POST /api/v1/billing/handoff ALSO returns a
    # ready-to-use redirect url = ``{base}/enter?t={token}`` so a caller need not
    # re-implement the frozen /enter?t= contract. EMPTY (default) => the response
    # carries only the raw token (the decoupled #1098 contract); self-hosted
    # deployments without an external billing service are unaffected (inert).
    # Set to the billing host origin, no path (e.g. https://billing.example.com).
    payment_public_base_url: str = Field(
        default="",
        description=(
            "Public base URL (origin, no path) of the external billing service's "
            "handoff entry. When set, /billing/handoff also returns a ready-to-use "
            "{base}/enter?t={token} redirect URL; empty (default) returns only the "
            "raw token. Opt-in convenience, inert when unset. Must be https (http "
            "only for localhost) with no path — validated at startup."
        ),
    )

    @field_validator("payment_public_base_url")
    @classmethod
    def _check_payment_public_base_url(cls, v: str) -> str:
        return _validate_handoff_base_url(v)

    # Public MCP base URL handed back to the worker in its config so it can write
    # memories. Defaults to local dev; override in prod (e.g. https://memory.kagura-ai.com/mcp).
    kmc_mcp_url: str = Field(
        default="http://localhost:8080/mcp",
        description="Public MCP base URL returned to ai-worker connectors for memory writes",
    )
    # Slack connector OAuth (shared Kagura Slack app). Empty client_id disables
    # the Slack connect flow (Spec 2026-06-02, Plan 4).
    slack_client_id: str = Field(default="", description="Slack app OAuth client id")
    slack_client_secret: str = Field(default="", description="Slack app OAuth client secret")
    slack_redirect_uri: str = Field(
        default="http://localhost:8080/api/v1/connectors/slack/callback",
        description="Slack OAuth redirect URI (must match the Slack app config)",
    )
    slack_oauth_scopes: str = Field(
        default="channels:history,channels:read,groups:history,chat:write,team:read,users:read",
        description="Comma-separated Slack bot scopes requested at install",
    )
    jwt_secret: str = Field(
        default="dev-secret-change-in-production", description="JWT signing secret (32+ bytes)"
    )
    jwt_algorithm: str = Field(default="HS256", description="JWT algorithm")
    jwt_expire_minutes: int = Field(default=60, description="JWT expiration (minutes)")
    audit_pseudo_salt: str = Field(
        default="kagura-erasure-v1",
        description="Per-deployment salt for audit_logs pseudonymization on account erasure (override in production)",
    )
    audit_hmac_key: str = Field(
        default="dev-audit-hmac-change-in-production",
        description=(
            "HMAC-SHA256 key for keyed hashing of mutable identifiers in "
            "audit_logs.old_value_hash / new_value_hash columns (Issue #481, "
            "OAuth email-sync events). Independent from API_KEY_SECRET so a "
            "rotation of either does not invalidate the other's audit trail. "
            "Override in production via AUDIT_HMAC_KEY (use: openssl rand -hex 32)."
        ),
    )

    # Session
    session_ttl_seconds: int = Field(
        default=7 * 24 * 60 * 60, description="Session TTL (default: 7 days)"
    )

    # API Settings
    api_host: str = Field(default="0.0.0.0", description="API host")
    api_port: int = Field(default=8080, description="API port")
    cors_origins: str = Field(
        default="http://localhost:3000,http://localhost:8080",
        description="CORS allowed origins (comma-separated)",
    )

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS origins from comma-separated string to list."""
        return [origin.strip() for origin in self.cors_origins.split(",")]

    frontend_url: str = Field(
        default="http://localhost:3000",
        description=(
            "Frontend application base URL. Used to compose user-facing "
            "links in transactional email content (e.g. the erasure "
            "confirmation link delivered to OAuth users in Issue #469). "
            "Set FRONTEND_URL to the deployed origin in production. The "
            "field is plain ``str`` rather than ``HttpUrl`` to match the "
            "existing ``os.getenv('FRONTEND_URL')`` call sites in "
            "``api/routes/auth.py`` and avoid f-string serialization "
            "differences — those raw call sites can be migrated in a "
            "separate cleanup once this field is established."
        ),
    )

    # Logging
    log_level: str = Field(default="INFO", description="Log level")
    log_colorize: bool = Field(default=True, description="Enable colored logs")

    # Qdrant Security (Issue #273 Security Review)
    qdrant_api_key: str = Field(
        default="", description="Qdrant API key (REQUIRED in production, empty for local dev)"
    )

    # Vector backend selection ("Kagura Lite" — embedded LanceDB backend).
    # Default "qdrant" preserves the server behavior exactly. "lance" routes
    # all vector ops through the in-process LanceDB store (db/lance_store.py),
    # intended for single-process self-hosted / CLI / desktop deployments.
    vector_backend: str = Field(
        default="qdrant",
        validation_alias=AliasChoices("KAGURA_VECTOR_BACKEND", "VECTOR_BACKEND"),
        description="Vector store backend: 'qdrant' (default, server) or 'lance' (embedded).",
    )
    lance_db_path: str = Field(
        default="./data/kagura.lance",
        validation_alias=AliasChoices("KAGURA_LANCE_DB_PATH", "LANCE_DB_PATH"),
        description="Filesystem path for the embedded LanceDB store (vector_backend=lance).",
    )

    # Embedding Configuration
    embedding_provider: str = Field(
        default="openai",
        description="Embedding model provider (openai/cohere/huggingface/self_hosted)",
    )

    @field_validator("embedding_provider", mode="before")
    @classmethod
    def _coerce_legacy_ollama_embedding_provider(cls, v: object) -> object:
        """Coerce the retired EMBEDDING_PROVIDER=ollama value to 'self_hosted' (#1160).

        The provider key was renamed ollama → self_hosted in a clean cutover,
        but .env.example documented 'ollama' as valid for many releases. Left
        verbatim, ``provider == "self_hosted"`` gates all miss and embedding
        traffic silently routes into the OpenAI client path (500 without a key,
        or wrong-model calls to api.openai.com with a platform key). Coerce with
        a boot warning instead of accepting the broken value (mirrors
        ``_warn_legacy_ollama_env`` for OLLAMA_BASE_URL).
        """
        if isinstance(v, str) and v.strip().lower() == "ollama":
            import warnings

            warnings.warn(
                "EMBEDDING_PROVIDER=ollama is retired (renamed to 'self_hosted' "
                "in #1160); coercing to 'self_hosted'. Update your env.",
                stacklevel=2,
            )
            return "self_hosted"
        return v

    embedding_model: str = Field(
        default="text-embedding-3-small", description="Embedding model name"
    )
    embedding_dimensions: int = Field(default=512, description="Embedding vector dimensions")
    # Issue #1030: when True, a workspace whose plan lacks the
    # ``managed_embeddings`` capability (Free / S) is DENIED the platform
    # ``OPENAI_API_KEY`` embedding fallback — it must supply a BYOK key or use a
    # self-hosted (self_hosted) model. Paid tiers (basic/pro) are unaffected: they keep
    # embedding on the platform key, bounded by the #709/#1033 spend cap.
    # Default False preserves the pre-#1030 behavior (env fallback for all tiers,
    # Free bounded only by the #708 drain-attack cap) so OSS / dev / self-host
    # deployments are unchanged; managed-SaaS flips this on once a dedicated
    # platform embedding credential is provisioned.
    embedding_platform_fallback_requires_managed_plan: bool = Field(
        default=False,
        description=(
            "Issue #1030: deny the platform OPENAI_API_KEY embedding fallback to "
            "plans without managed_embeddings (Free). Paid tiers keep platform "
            "embeddings (capped). Default False = pre-#1030 behavior."
        ),
    )

    # Issue #886: hard cap on the deterministic always-load (load_pinned) set.
    # always-load memories are injected into the agent's context every turn, so
    # the cap is a safety valve against runaway pinning. When the pinned set
    # exceeds it, the read is bounded and the response flags truncated=true with
    # the true total_available (never silently dropped).
    pinned_load_cap: int = Field(
        default=100,
        description="Max memories returned by the deterministic always-load path (#886)",
    )

    # Self-hosted inference configuration (Issue #44, renamed #1160)
    # Any OpenAI-compatible backend (Ollama default port 11434, vLLM default 8000).
    self_hosted_base_url: str = Field(
        default="http://localhost:11434",
        description="Self-hosted OpenAI-compatible backend base URL (Ollama, vLLM, ...)",
    )
    self_hosted_api_key: str = Field(
        default="",
        description=(
            "Optional bearer token for a self-hosted backend that requires one "
            "(e.g. vLLM launched with --api-key). Ollama ignores it."
        ),
    )

    @field_validator("self_hosted_base_url")
    @classmethod
    def _strip_self_hosted_base_url(cls, v: str) -> str:
        """Normalize a trailing slash so consumers can join ``/v1/...`` safely.

        Operators commonly set ``SELF_HOSTED_BASE_URL`` with a trailing slash;
        without this, ``f"{base}/v1/models"`` becomes ``http://host//v1/models``
        which 404s on some servers and misreports the backend as down. Strip
        once at the source so every consumer (embedding probe, telemetry probe,
        SelfHostedProvider/Reranker) gets a clean base.
        """
        return v.rstrip("/") if v else v

    # Local reranker serving override
    rerank_base_url: str = Field(
        default="",
        description=(
            "OpenAI/Jina-style /v1/rerank endpoint (vLLM --runner pooling, TEI, "
            "or Infinity) for the local reranker path. When set, the context-"
            "config 'self_hosted' reranker posts ONE batched /v1/rerank request "
            "here instead of per-document prompt scoring. Empty = keep the "
            "prompt-scoring SelfHostedReranker on self_hosted_base_url."
        ),
    )
    rerank_model: str = Field(
        default="qwen3-reranker-0.6b",
        description=(
            "Served model name for the local /v1/rerank endpoint (RERANK_BASE_URL). "
            "Must match the endpoint's model id exactly — e.g. vLLM's "
            "--served-model-name, or the TEI/Infinity model id. This is the "
            "ops-level default; a per-context reranker_model (when set and not a "
            "stale remote-provider default) still overrides it. Only consulted "
            "when rerank_base_url is set. Keep in sync with "
            "reranker_service.DEFAULT_VLLM_RERANK_MODEL."
        ),
    )
    rerank_api_key: str = Field(
        default="",
        description=(
            "Optional bearer token for the /v1/rerank endpoint (RERANK_BASE_URL) "
            "when it is served behind auth (e.g. vLLM launched with --api-key). "
            "Sent as 'Authorization: Bearer ...' by VLLMReranker; empty = no header."
        ),
    )
    self_hosted_rerank_model: str = Field(
        default="dengcao/Qwen3-Reranker-8B:Q5_K_M",
        description=(
            "Default model for the prompt-scoring SelfHostedReranker on "
            "SELF_HOSTED_BASE_URL (the /v1/completions path, used when "
            "RERANK_BASE_URL is unset). Defaults to the Ollama-registry id; a "
            "pure-vLLM self-hosted stack must override it (env "
            "SELF_HOSTED_RERANK_MODEL) with a model the backend actually serves "
            "so scoring does not 404 to all-zero relevance. Keep in sync with "
            "reranker_service.DEFAULT_SELF_HOSTED_RERANK_MODEL."
        ),
    )

    @field_validator(
        "rerank_base_url",
        "rerank_model",
        "rerank_api_key",
        "self_hosted_rerank_model",
        mode="before",
    )
    @classmethod
    def _strip_rerank_fields(cls, v: object) -> object:
        # rerank_base_url is used as a truthy feature toggle
        # (`if settings.rerank_base_url:`) and rerank_model is sent verbatim to
        # the endpoint. Strip surrounding whitespace and collapse a
        # whitespace-only value to "" so a stray-space env var neither enables
        # the vLLM path with an invalid URL nor ships a blank model name.
        return v.strip() if isinstance(v, str) else v

    # Search Configuration (Issue #105)
    enable_reranking: bool = Field(
        default=True,
        description="Enable reranking globally. Future: toggle external vs local reranker.",
    )

    # Feature Flags
    enable_neural_memory: bool = Field(default=False, description="Enable Neural Memory (Phase 3)")
    enable_research_tools: bool = Field(
        default=True, description="Enable research tools (Brave Search etc.)"
    )
    enable_plan_page: bool = Field(
        default=False,
        description=(
            "Enable the workspace Plan page + its sidebar nav entry (#1145). OFF "
            "by default for OSS / self-hosted: pricing lives on the payment side "
            "and billing is unconfigured by default, so the Plan surface only "
            "makes sense where billing is wired up. Managed SaaS sets this true. "
            "Surfaced to the frontend via GET /api/v1/system/info features.plan_page."
        ),
    )
    enable_byok: bool = Field(
        default=True,
        description=(
            "Enable BYOK (bring-your-own-key) PROVISIONING (#1167). ON by default "
            "so OSS / self-hosted keeps external API key management. When false, "
            "the /external-keys WRITE paths (create/update), the workspace cost "
            "dashboard (GET /workspaces/{id}/cost-aggregation), and the OpenAI "
            "key-status probe return 404 and the web UI hides their nav entries. "
            "v0.42 review #32: key MANAGEMENT (list / toggle-off / delete) stays "
            "reachable so an owner can still disable/delete an already-stored key. "
            "Key RESOLUTION is also untouched: keys stored before the flag flip "
            "are still used by the LLM/embedding services (so live embeddings do "
            "not break) — the customer removes them via the still-open management "
            "paths. Surfaced to the frontend via GET /api/v1/system/info features.byok."
        ),
    )
    enable_referrals: bool = Field(
        default=False,
        description=(
            "Enable the referral program (#1470) — a user invites another user "
            "and both sides earn a memory-quota bonus. OFF by default: it mints "
            "entitlement, so it must be an explicit opt-in for OSS / self-hosted. "
            "This is also the KILL SWITCH: flipping it false stops all new "
            "redemptions (already-granted bonuses are unaffected and are reversed "
            "individually via the admin revoke endpoint). Surfaced to the frontend "
            "via GET /api/v1/system/info features.referrals."
        ),
    )
    # Issue #1470: referral reward tuning. The ``le=`` bounds are the first
    # safety rail — an operator typo becomes a fail-closed startup error rather
    # than a payout incident. They are NOT sufficient on their own: per-field
    # bounds cannot express a *product* constraint, and the product of these
    # ceilings (10 x 2000 = 20000) would blow past the FREE -> BASIC memory gap.
    # ``_validate_referral_payout_budget`` below is the guard that actually
    # holds the tier ladder; see REFERRAL_TOTAL_PAYOUT_BUDGET_MEMORIES.
    #
    # The referrer and referee amounts are SEPARATE fields on purpose — setting
    # the referrer side to 0 during an abuse burst removes the farming incentive
    # while the onboarding gift keeps working, which a single on/off flag cannot
    # express.
    referral_referee_reward_memories: int = Field(
        default=500,
        ge=0,
        le=2000,
        description="Memory-quota bonus granted to the invited user on redemption (#1470).",
    )
    referral_referrer_reward_memories: int = Field(
        default=500,
        ge=0,
        le=2000,
        description="Memory-quota bonus granted to the inviter per successful referral (#1470).",
    )
    referral_max_grants_per_referrer: int = Field(
        default=3,
        ge=0,
        le=10,
        description=(
            "Lifetime cap on successful referrals per inviter (#1470). Launched at "
            "3 rather than 5 because raising the cap is non-breaking while lowering "
            "it strands users who already earned the higher slots."
        ),
    )
    referral_redeem_window_hours: int = Field(
        default=72,
        ge=1,
        le=720,
        description=(
            "How long after signup a user may redeem a referral code (#1470). This "
            "is the 'is this genuinely a new user' check: it is cheap, observable "
            "from users.created_at, and it stops an established account from "
            "redeeming a code long after the fact."
        ),
    )
    enable_managed_connectors: bool = Field(
        default=False,
        description=(
            "Managed-connectors (hosted SaaS) mode for the chat-connector UI (#1426). "
            "OFF by default for OSS / self-hosted, where the operator runs their own "
            "worker: the connectors page shows the 'link existing Slack app' (BYO) "
            "form and treats a per-connector LLM as required. When true (managed "
            "SaaS), the shared worker/bridge provides the pre-compile LLM and only "
            "the OAuth path is offered, so the web UI hides the BYO manual-bind form "
            "and stops flagging a missing per-connector LLM. Surfaced to the frontend "
            "via GET /api/v1/system/info features.managed_connectors."
        ),
    )
    enable_owner_key_member_management: bool = Field(
        default=True,
        description=(
            "v0.42 review #33: gate for owner-API-key member/credential management "
            "(#1164/#1165 — POST /workspaces/{id}/members, invitations, and "
            "owner-provisioned member API keys). ON by default preserves the "
            "shipped behavior. Set false on deployments that do not want an "
            "owner's ordinary read/write API key to ALSO carry member-management "
            "power (a stolen owner key would otherwise mint member keys / add "
            "members); session owners are unaffected. This is a deployment-level "
            "kill-switch pending per-key scopes."
        ),
    )

    # Environment
    environment: str = Field(
        default="development", description="Environment (development/staging/production)"
    )

    # OAuth Provider Control (Issue #360)
    auth_providers: str = Field(
        default="auto",
        description="Comma-separated OAuth providers to enable: 'google', 'github', 'google,github', or 'auto' (enable all configured)",
    )

    # Registration Control (Issue #349)
    allow_registration: bool = Field(
        default=False,
        description="Allow new user registration via OAuth. "
        "When False, only invited users and the first admin can register.",
    )

    # Workspace Governance (Issue #1113)
    require_dual_control_force_transfer: bool = Field(
        default=False,
        description="Require a second, distinct system admin to approve a break-glass "
        "ownership force-transfer before it commits (four-eyes). When False (default), "
        "force-transfer commits immediately as today. Enabling this needs >=2 system "
        "admins — with only one, no force-transfer can be approved.",
    )

    # In-process Stripe billing settings removed (#1096): the OSS backend is
    # Stripe-agnostic. Entitlement arrives via the internal billing endpoint
    # (BILLING_SERVICE_TOKEN-authed); no Stripe SDK, keys, or webhook secret here.

    # Transactional email (Issue #478 — unblocks #469's OAuth confirm-email path)
    email_provider: Literal["logging", "resend"] = Field(
        default="logging",
        description=(
            "Active EmailService backend. 'logging' writes structured log "
            "lines for ops to forward manually (closed-beta default). "
            "'resend' delivers via Resend (requires resend_api_key). "
            "Pydantic rejects other values at boot, so EMAIL_PROVIDER typos "
            "fail loudly instead of silently falling back to logging."
        ),
    )
    resend_api_key: str | None = Field(
        default=None,
        description="Resend API key (required when email_provider='resend')",
    )
    resend_from_email: str = Field(
        default="noreply@kagura-ai.com",
        description=(
            "From address for transactional email. Must match a verified "
            "sending domain on Resend; otherwise sends 403."
        ),
    )
    resend_dpa_accepted_at: datetime | None = Field(
        default=None,
        description=(
            "ISO 8601 timestamp when ops accepted the Resend Data Processing "
            "Addendum (https://resend.com/legal/dpa). Required when "
            "email_provider='resend' as a defense-in-depth GDPR Art.28 control: "
            "without an accepted DPA on record, Resend cannot legally process "
            "personal data on our behalf, so the boot-time validator refuses "
            "to start the app."
        ),
    )

    # Memory Analysis allowlist (Issue #496) — kill switch.
    # Empty (default) → feature 403 globally. Comma-separated UUIDs → only listed
    # workspaces can run analyses. Env: ANALYSIS_ENABLED_WORKSPACE_IDS.
    # Production deploy starts empty; ops adds the kagura-dev workspace UUID first
    # then expands gradually. Survives the new tier+quota+BYOK gates so a single
    # env edit can stop all runs in production. v1.5 で撤去予定.
    analysis_enabled_workspace_ids: str = Field(
        default="",
        description=(
            "Comma-separated workspace UUIDs allowed to run memory analyses. "
            "Empty = feature OFF globally (Issue #496 kill switch). "
            "Populated = only listed workspaces. v1.5 で撤去予定."
        ),
    )

    @property
    def analysis_enabled_workspace_ids_list(self) -> list[str]:
        """Parse comma-separated UUIDs into a list of lowercase strings.

        Returns empty list when the setting is empty/whitespace, which the
        gate translates into "feature 403 globally" (kill switch active).
        UUIDs are kept as strings here; the gate compares them against
        ``str(workspace_id).lower()`` so neither side has to construct UUID
        objects on the hot path. Lower-casing both sides defends against
        ops setting upper-case UUIDs in the env var (Python's ``str(UUID)``
        always emits lower-case hex), which would otherwise silent-fail
        with a permanent 403.
        """
        return [
            s.strip().lower() for s in self.analysis_enabled_workspace_ids.split(",") if s.strip()
        ]

    # Memory Analysis run-size cap (Issue #1244).
    # The pipeline materializes every matching vector into one in-RAM numpy
    # matrix and runs UMAP/KMeans inside the API process (the Option B
    # analysis-worker externalization is not yet implemented), so run size
    # directly bounds API-container memory/CPU. Default 10_000 = the
    # basic-tier memory_limit (plan_tiers.py); ~10k × 3072 dims × 4 B
    # ≈ 123 MB float32 plus UMAP's kNN working set — tolerable for one run,
    # and the #1240 one-running-run index plus the 3/day quota bound
    # concurrency. Env: ANALYSIS_MAX_MEMORY_COUNT.
    analysis_max_memory_count: int = Field(
        default=10_000,
        ge=1,
        description=(
            "Hard cap on memories per Memory Analysis run. Contexts above "
            "this are rejected at preview and start (422 naming the limit). "
            "Self-host operators with larger containers may raise it. "
            "Must be >= 1 — a zero/negative value would brick the feature "
            "at runtime with a misleading message, so it fails at boot."
        ),
    )

    # BM25 IDF Drift (Issue #343, #378)
    bm25_drift_cron_enabled: bool = Field(
        default=False,
        description="Enable BM25 IDF drift cron in production (Issue #378)",
    )
    bm25_reveal_rate_limit_per_hour: int = Field(
        default=10,
        description="Max BM25 reveal-terms calls per user per hour (Issue #377)",
    )

    # Plan Tier Overrides (environment variable customization for OSS deployments)
    plan_free_max_contexts: int | None = Field(
        default=None, description="Override FREE plan max contexts per workspace"
    )
    plan_free_memory_limit: int | None = Field(
        default=None, description="Override FREE plan memory limit"
    )
    plan_free_mcp_calls_per_day: int | None = Field(
        default=None, description="Override FREE plan MCP calls/day"
    )
    plan_basic_max_contexts: int | None = Field(
        default=None, description="Override BASIC plan max contexts per workspace"
    )
    plan_basic_memory_limit: int | None = Field(
        default=None, description="Override BASIC plan memory limit"
    )
    plan_basic_mcp_calls_per_day: int | None = Field(
        default=None, description="Override BASIC plan MCP calls/day"
    )
    plan_pro_max_contexts: int | None = Field(
        default=None, description="Override PRO plan max contexts per workspace"
    )
    plan_pro_memory_limit: int | None = Field(
        default=None, description="Override PRO plan memory limit"
    )
    plan_pro_mcp_calls_per_day: int | None = Field(
        default=None, description="Override PRO plan MCP calls/day"
    )
    plan_free_storage_limit_bytes: int | None = Field(
        default=None, description="Override FREE plan file-storage hard cap (bytes, Issue #485)"
    )
    plan_basic_storage_limit_bytes: int | None = Field(
        default=None, description="Override BASIC plan file-storage hard cap (bytes, Issue #485)"
    )
    plan_pro_storage_limit_bytes: int | None = Field(
        default=None, description="Override PRO plan file-storage hard cap (bytes, Issue #485)"
    )
    plan_free_sleep_enabled_contexts_limit: int | None = Field(
        default=None,
        description="Override FREE plan sleep-enabled contexts cap (Issue #560; default 0)",
    )
    plan_basic_sleep_enabled_contexts_limit: int | None = Field(
        default=None,
        description="Override BASIC plan sleep-enabled contexts cap (Issue #560; default 0)",
    )
    plan_pro_sleep_enabled_contexts_limit: int | None = Field(
        default=None,
        description="Override PRO plan sleep-enabled contexts cap (Issue #560; default 3)",
    )
    # Issue #709: Per-workspace BYOK embedding spend caps (USD). ``None`` =
    # use the dataclass default in ``plan_tiers.py``. Float because the cap
    # is a USD value (allows sub-dollar caps for Free tier).
    plan_free_embedding_daily_cap_usd: float | None = Field(
        default=None,
        description="Override FREE plan daily embedding spend cap in USD (Issue #709; default 0.50)",
    )
    plan_free_embedding_monthly_cap_usd: float | None = Field(
        default=None,
        description="Override FREE plan monthly embedding spend cap in USD (Issue #709; default 15.0)",
    )
    plan_basic_embedding_daily_cap_usd: float | None = Field(
        default=None,
        description="Override BASIC plan daily embedding spend cap in USD (Issue #709; default 2.0)",
    )
    plan_basic_embedding_monthly_cap_usd: float | None = Field(
        default=None,
        description="Override BASIC plan monthly embedding spend cap in USD (Issue #709; default 60.0)",
    )
    plan_pro_embedding_daily_cap_usd: float | None = Field(
        default=None,
        description="Override PRO plan daily embedding spend cap in USD (Issue #709; default 10.0)",
    )
    plan_pro_embedding_monthly_cap_usd: float | None = Field(
        default=None,
        description="Override PRO plan monthly embedding spend cap in USD (Issue #709; default 300.0)",
    )
    # Issue #661's ``plan_*_max_owned_workspaces`` env overrides were removed
    # in #675 — the cap is now per-user (``users.workspace_slot_bonus``) and
    # no longer depends on the plan tier.
    enforce_workspace_cap: bool = Field(
        default=False,
        description=(
            "Issue #661 / #675 rollout gate. When False (default), workspace "
            "creation is allowed unconditionally but a structured warn log "
            "is emitted whenever a user is over their effective cap "
            "(1 + workspace_slot_bonus). Flip to True once the cleanup "
            "window closes — tracked in #677 (sub-C: TOCTOU + enforce flip)."
        ),
    )

    # Plan Display Names (customizable for SaaS forks)
    plan_free_display_name: str | None = Field(
        default=None, description="Display name for FREE tier (default: S)"
    )
    plan_basic_display_name: str | None = Field(
        default=None, description="Display name for BASIC tier (default: M)"
    )
    plan_pro_display_name: str | None = Field(
        default=None, description="Display name for PRO tier (default: L)"
    )

    # Usage warning thresholds (Issue #48)
    usage_warning_threshold: float = Field(
        default=0.80, description="Usage warning threshold (0.0-1.0)"
    )
    usage_critical_threshold: float = Field(
        default=0.95, description="Usage critical threshold (0.0-1.0)"
    )

    # Object Storage — S3-compatible backend (Issue #485 R2; generalized #994).
    # All five credential settings accept STORAGE_* (canonical), S3_*, and R2_*
    # (legacy) env vars via AliasChoices. The canonical name is listed FIRST so
    # it wins when more than one is set; R2_* is kept so existing prod deploys
    # (which set only R2_*) work unchanged. `Settings` uses extra="ignore", so
    # dropping the r2_* alias would SILENTLY ignore prod's R2_* env -> empty
    # endpoint -> the upload path 502s with no load-time error (#994 gate1 risk).
    storage_backend_type: Literal["r2", "s3", "minio", "s3-compatible", "aws"] = Field(
        default="r2",
        description=(
            "Object-storage backend discriminator. All use the same "
            "S3-compatible client; only the endpoint (and, for 'aws', the "
            "region) differs. Default 'r2' preserves the managed-cloud deploy "
            "(Cloudflare R2). Self-hosters set 'minio' / 's3' / 'aws'. Pydantic "
            "rejects other values at boot, so STORAGE_BACKEND_TYPE typos fail "
            "loudly instead of 502-ing on the first upload."
        ),
    )
    storage_region: str = Field(
        default="auto",
        validation_alias=AliasChoices("storage_region", "s3_region", "r2_region"),
        description=(
            "S3 region. 'auto' (default) is correct for R2 and MinIO (both "
            "ignore it). Set the bucket's real region (e.g. 'us-east-1') for an "
            "'aws' backend, where SigV4 needs it."
        ),
    )
    storage_account_id: str = Field(
        default="",
        validation_alias=AliasChoices("storage_account_id", "s3_account_id", "r2_account_id"),
        description="S3-compatible account ID (R2 account ID; unused by AWS S3 / MinIO)",
    )
    storage_access_key_id: str = Field(
        default="",
        validation_alias=AliasChoices(
            "storage_access_key_id", "s3_access_key_id", "r2_access_key_id"
        ),
        description="S3-compatible access key ID",
    )
    storage_secret_access_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "storage_secret_access_key",
            "s3_secret_access_key",
            "r2_secret_access_key",
        ),
        description="S3-compatible secret access key",
    )
    storage_bucket: str = Field(
        default="",
        validation_alias=AliasChoices("storage_bucket", "s3_bucket", "r2_bucket"),
        description="Bucket name (e.g. kagura-memory-files-dev)",
    )
    storage_endpoint_url: str = Field(
        default="",
        validation_alias=AliasChoices("storage_endpoint_url", "s3_endpoint_url", "r2_endpoint_url"),
        description=(
            "S3-compatible endpoint URL. R2: https://<account>.r2.cloudflarestorage.com; "
            "MinIO: http://minio:9000; AWS S3: leave empty for the default region endpoint."
        ),
    )
    file_object_max_size_mb: int = Field(
        default=100, description="Per-file size cap in MB (Issue #485 Phase 1 = 100)"
    )
    presign_put_ttl_seconds: int = Field(
        default=300, description="Presigned PUT URL lifetime in seconds (Issue #485 R4)"
    )
    presign_get_ttl_seconds: int = Field(
        default=300, description="Presigned GET URL lifetime in seconds"
    )
    storage_checksum_binding_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "storage_checksum_binding_enabled",
            "s3_checksum_binding_enabled",
            "r2_checksum_binding_enabled",
        ),
        description=(
            "Bind body sha256 to presigned PUT via S3 ChecksumSHA256 (Issue #556). "
            "When True, R2 verifies the body sha256 server-side and rejects "
            "mismatched bytes with HTTP 400 BadDigest — closes the per-workspace "
            "dedup-poisoning gap from #485 Phase 1. REQUIRES the SDK to send the "
            "x-amz-checksum-sha256 header (kagura-memory-python-sdk >= 0.14.0, "
            "FilesClient shipped 2026-05-11); older SDKs (v0.13.x and below) "
            "will fail with 403 SignatureDoesNotMatch. "
            "Default False keeps the Phase 1 behavior so backend can deploy "
            "ahead of SDK rollout — operators flip to True after their tenants "
            "have updated. Plan to default True once SDK adoption stabilizes; "
            "criteria for the upstream default flip and eventual flag removal "
            "are codified in docs/ops/r2-checksum-binding-rollout.md §10 (#577)."
        ),
    )
    allowed_file_content_types: str = Field(
        default=(
            "image/png,image/jpeg,image/gif,application/pdf,"
            "text/plain,text/markdown,text/csv,application/json"
        ),
        description=(
            "Comma-separated MIME allow-list for /api/v1/files/* and the MCP "
            "file tools (Issue #553). Comparison is case-insensitive (RFC 6838). "
            "Empty/whitespace = deny all (fail-closed); rely on the default for "
            "the closed-beta MIME set or override via ALLOWED_FILE_CONTENT_TYPES "
            "to widen / narrow per deployment."
        ),
    )

    @property
    def allowed_file_content_types_set(self) -> set[str]:
        """Parsed allow-list (bare type/subtype, lower-cased). Empty = fail-closed.

        Mirrors ``FileStorageService.reserve_upload``'s parameter-stripping so
        an operator value like ``text/plain; charset=utf-8`` in the env var
        normalizes to ``text/plain`` and matches an upload that strips the
        same parameters at compare time. Boot-time validation
        (``_validate_allowed_file_content_types``) rejects malformed entries
        so this set never contains shapes that would silently never match.
        """
        return {
            normalized
            for s in self.allowed_file_content_types.split(",")
            if (normalized := normalize_media_type(s))
        }

    @model_validator(mode="after")
    def _validate_allowed_file_content_types(self) -> "Settings":
        """Boot-time fail-fast on malformed ``ALLOWED_FILE_CONTENT_TYPES`` entries.

        Empty / whitespace-only entries pass through (filtered out), so
        ``ALLOWED_FILE_CONTENT_TYPES=""`` still resolves to fail-closed
        (empty set, every upload rejected) without crashing the app.
        Malformed shapes (no slash, empty type/subtype, garbage chars) crash
        boot — they would otherwise leak into ``details["allowed"]`` of every
        ``UnsupportedMediaTypeError`` response, exposing the misconfiguration
        to clients without the operator noticing.
        """
        invalid: list[str] = []
        for entry in (self.allowed_file_content_types or "").split(","):
            if not entry.strip():
                continue
            normalized = normalize_media_type(entry)
            if not normalized or not MEDIA_TYPE_RE.match(normalized):
                invalid.append(entry.strip())
        if invalid:
            raise ValueError(
                "ALLOWED_FILE_CONTENT_TYPES has malformed entries "
                f"(must be 'type/subtype'): {invalid!r}"
            )
        return self

    @field_validator("resend_dpa_accepted_at", mode="before")
    @classmethod
    def _coerce_blank_dpa_to_none(cls, v: Any) -> Any:
        """Treat blank/whitespace RESEND_DPA_ACCEPTED_AT as unset.

        Without this, pydantic's strict datetime parser rejects an empty or
        whitespace-only value with "Input should be a valid datetime" before
        the model_validator runs, bypassing the friendly DPA-URL-bearing
        error message in _validate_resend_config. Stripping here mirrors the
        .strip() parity treatment on resend_api_key / resend_from_email.
        """
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def _validate_resend_config(self) -> "Settings":
        """Fail-fast at Settings load when EMAIL_PROVIDER=resend is misconfigured.

        Without this, EMAIL_PROVIDER=resend with no RESEND_API_KEY would boot
        successfully and only fail at first email send — a delayed failure
        mode that masks deployment-time config errors. Same intent for
        RESEND_FROM_EMAIL: an empty/whitespace value would let the service
        construct, then Resend would reject the first send with an opaque
        4xx. Surfacing both here means misconfig surfaces in the lifespan
        traceback, not in a request handler.
        """
        if self.email_provider == "resend":
            if not (self.resend_api_key or "").strip():
                raise ValueError("EMAIL_PROVIDER=resend requires RESEND_API_KEY to be set")
            if not (self.resend_from_email or "").strip():
                raise ValueError(
                    "EMAIL_PROVIDER=resend requires RESEND_FROM_EMAIL to be a non-empty address"
                )
            if self.resend_dpa_accepted_at is None:
                raise ValueError(
                    "EMAIL_PROVIDER=resend requires RESEND_DPA_ACCEPTED_AT to be set "
                    "(ISO 8601 timestamp when ops accepted the Resend DPA — "
                    "see https://resend.com/legal/dpa)"
                )
        return self

    @model_validator(mode="after")
    def _validate_referral_payout_budget(self) -> "Settings":
        """Fail-fast when the referral config could close the FREE -> BASIC gap (#1470).

        The per-field ``le=`` bounds cannot express this: they bound each knob
        independently, but the thing that matters is the PRODUCT
        ``max_grants x reward``. At the individual ceilings that product reaches
        10 x 2000 = 20000, more than double the 9000-memory gap — i.e. an
        operator could hand out the paid tier for free without ever exceeding a
        single field's bound.

        The formula counts what actually accrues to ONE workspace, which is what
        ``ReferralService._recompute_workspace_bonus`` sums: a user is the
        referee at most once (``UNIQUE(referred_user_id)``) and the referrer up
        to ``max_grants`` times. Using ``max(referrer, referee)`` instead would
        undercount by a whole referee reward and let an in-bounds config close
        the gap anyway.

        Only enforced when the program is enabled, so an OSS deployment that
        never turns referrals on is not forced to keep the numbers coherent.
        """
        if not self.enable_referrals:
            return self
        worst_case = (
            self.referral_max_grants_per_referrer * self.referral_referrer_reward_memories
            + self.referral_referee_reward_memories
        )
        # EXCLUSIVE bound: the budget IS the gap, so equality means a
        # fully-used chain lands exactly ON the BASIC limit — the paid tier,
        # for free. ">=" is what makes "cannot reach BASIC" true rather than
        # "cannot exceed BASIC".
        if worst_case >= REFERRAL_TOTAL_PAYOUT_BUDGET_MEMORIES:
            raise ValueError(
                "Referral config would mint up to "
                f"{worst_case} memories per inviter, reaching the "
                f"{REFERRAL_TOTAL_PAYOUT_BUDGET_MEMORIES}-memory budget "
                "(the FREE->BASIC gap). Lower REFERRAL_MAX_GRANTS_PER_REFERRER "
                "or the reward amounts — otherwise a fully-used referral chain "
                "hands out the paid tier for free."
            )
        return self

    @model_validator(mode="after")
    def _warn_legacy_ollama_env(self) -> "Settings":
        """Warn (do not honor) when the retired OLLAMA_BASE_URL env is set (#1160).

        The self-hosted base URL env was renamed OLLAMA_BASE_URL →
        SELF_HOSTED_BASE_URL in a clean cutover (no read alias). A deployment
        that still sets the old var *and* has not configured the new one would
        silently fall back to the default, so surface it loudly at boot rather
        than failing mysteriously later.

        The guard checks ``model_fields_set`` (not ``os.getenv``) for the new
        field: pydantic-settings loads .env-file values into the model without
        injecting them into ``os.environ``, so an ``os.getenv`` check would
        false-warn when SELF_HOSTED_BASE_URL is supplied via a .env file.
        """
        if os.getenv("OLLAMA_BASE_URL") and "self_hosted_base_url" not in self.model_fields_set:
            import warnings

            warnings.warn(
                "OLLAMA_BASE_URL is set but no longer read (renamed to "
                "SELF_HOSTED_BASE_URL in #1160). Rename it in your env; the old "
                "value is ignored and the self-hosted base URL falls back to the "
                "default.",
                stacklevel=2,
            )
        return self


# Global settings instance
_settings: Settings | None = None


def get_settings() -> Settings:
    """Get global settings instance.

    Returns:
        Settings instance

    Raises:
        ValueError: If required environment variables are missing
    """
    global _settings

    if _settings is None:
        _settings = Settings()

    return _settings
