"""Validated non-secret runtime controls vended to connector workers (#1348)."""

from __future__ import annotations

import string
from typing import Literal, cast, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# #1377: the worker Locale contract, mirroring the connector worker's
# WorkerConfigResponse.locale (Literal["en", "ja"]). A non-conforming vended
# value fails bridge-side validation of the WHOLE config body — the tenant
# fails closed with only a generic config_unavailable in the logs. This is the
# single source shared by the admin write boundary (strict: 422), the vend
# read boundary (lenient: degrade to None), and the OpenAPI schema. See
# docs/connector-ingest-contract.md for the cross-repo contract.
WorkerLocale = Literal["en", "ja"]
# Derived from the Literal so the runtime normalizer and the type/OpenAPI
# contract can never disagree.
WORKER_LOCALES: tuple[str, ...] = get_args(WorkerLocale)


def normalize_worker_locale(value: str | None) -> WorkerLocale | None:
    """Normalize a connector locale to the worker Locale contract.

    Maps common BCP-47 forms to their primary subtag (``ja-JP`` → ``ja``,
    ``en_GB`` → ``en``, case-insensitive). Blank/None normalizes to ``None``
    (worker default).

    Raises:
        ValueError: when the primary subtag is not in ``WORKER_LOCALES``.
    """
    if value is None:
        return None
    primary = value.strip().replace("_", "-").split("-", 1)[0].lower()
    if not primary:
        return None
    if primary not in WORKER_LOCALES:
        raise ValueError(
            f"locale must map to one of {list(WORKER_LOCALES)} "
            f"(the worker Locale contract); got {value!r}"
        )
    return cast(WorkerLocale, primary)


# Upper bounds on tenant-writable knobs (#1350 review). These controls tune a
# SHARED worker process (its Redis, its supervisor loop, its shutdown budget),
# so an unbounded value is a cross-tenant resource lever: max_len/volume caps
# bound Redis memory, tick floors/caps bound the supervisor duty cycle, and
# the shutdown budget cap keeps deploy rollouts drainable. Floats additionally
# reject Infinity/NaN (stdlib json accepts the bare Infinity literal, gt=0
# passes it, and PostgreSQL jsonb then rejects the flush with a 500).
_MAX_BUFFER_LEN = 100_000
_MAX_VOLUME_TOKENS = 1_000_000
_MAX_TRACKED_TOPICS = 100_000
_MAX_SILENCE_SECONDS = 7 * 86400
_MIN_TICK_SECONDS = 0.1
_MAX_TICK_SECONDS = 3600.0
_MAX_SHUTDOWN_FLUSH_TIMEOUT = 600.0
_MAX_ANSWER_TIMEOUT = 300.0
_MAX_TIME_WINDOW_MINUTES = 7 * 24 * 60
_MAX_SUMMARY_CHARS = 512
_MAX_LINK_TEMPLATE_CHARS = 1024


class WorkerRuntimeBufferConfig(BaseModel):
    """Tenant-owned buffer limits; connection coordinates are intentionally absent."""

    model_config = ConfigDict(extra="forbid")

    ttl_seconds: int = Field(default=86400, gt=0, le=86400)
    max_len: int = Field(default=10_000, gt=0, le=_MAX_BUFFER_LEN)


class WorkerRuntimeFlushConfig(BaseModel):
    """Per-connector topic flush thresholds."""

    model_config = ConfigDict(extra="forbid")

    silence_seconds: int = Field(default=300, ge=0, le=_MAX_SILENCE_SECONDS)
    volume_tokens: int = Field(default=2000, gt=0, le=_MAX_VOLUME_TOKENS)
    max_tracked_topics: int = Field(default=10_000, gt=0, le=_MAX_TRACKED_TOPICS)


class WorkerRuntimeSupervisorConfig(BaseModel):
    """Per-connector supervisor cadence and shutdown budget."""

    model_config = ConfigDict(extra="forbid")

    tick_seconds: float = Field(
        default=1.0, ge=_MIN_TICK_SECONDS, le=_MAX_TICK_SECONDS, allow_inf_nan=False
    )
    shutdown_flush_timeout_seconds: float = Field(
        default=25.0, gt=0, le=_MAX_SHUTDOWN_FLUSH_TIMEOUT, allow_inf_nan=False
    )


class WorkerRuntimeLifecycleConfig(BaseModel):
    """Connector lifecycle propagation behavior."""

    model_config = ConfigDict(extra="forbid")

    deletion_mode: Literal["forget", "redact"] = "redact"
    redacted_summary: str = Field(
        default="[redacted on user request]", min_length=1, max_length=_MAX_SUMMARY_CHARS
    )
    dormant_summary: str = Field(
        default="[dormant] (channel archived)", min_length=1, max_length=_MAX_SUMMARY_CHARS
    )
    # Sentinel written when a source message is EDITED, distinct from the
    # deletion one so the record does not claim the message was removed while it
    # still exists upstream.
    edited_summary: str = Field(
        default="[stale] (source message edited)", min_length=1, max_length=_MAX_SUMMARY_CHARS
    )


class WorkerRuntimeContinuityConfig(BaseModel):
    """Per-connector topic-continuity thresholds."""

    model_config = ConfigDict(extra="forbid")

    time_window_minutes: int = Field(default=30, ge=0, le=_MAX_TIME_WINDOW_MINUTES)
    semantic_threshold: float = Field(default=0.6, ge=0.0, le=1.0, allow_inf_nan=False)
    semantic_check_enabled: bool = False


class WorkerRuntimeConfig(BaseModel):
    """Complete non-secret, installation-scoped worker runtime contract."""

    model_config = ConfigDict(extra="forbid")

    buffer: WorkerRuntimeBufferConfig = Field(default_factory=WorkerRuntimeBufferConfig)
    flush: WorkerRuntimeFlushConfig = Field(default_factory=WorkerRuntimeFlushConfig)
    supervisor: WorkerRuntimeSupervisorConfig = Field(default_factory=WorkerRuntimeSupervisorConfig)
    lifecycle: WorkerRuntimeLifecycleConfig = Field(default_factory=WorkerRuntimeLifecycleConfig)
    continuity: WorkerRuntimeContinuityConfig = Field(default_factory=WorkerRuntimeContinuityConfig)
    vision_enabled: bool = True
    # Both default OFF and are DORMANT capabilities on the consumer side: each
    # one, once enabled, changes what the consumer returns or ingests, so the
    # flip is an operational decision taken per connector after measuring the
    # impact. This model must carry them or they cannot be set at all — the
    # config is `extra="forbid"`, so an unknown key is a 422 rather than a
    # forward-compatible pass-through.
    team_scope_filter_enabled: bool = False
    channel_allowlist_enabled: bool = False
    mention_answer_enabled: bool = False
    answer_relevance_threshold: float = Field(default=0.35, ge=0.0, le=1.0, allow_inf_nan=False)
    answer_timeout_sec: float = Field(
        default=8.0, gt=0.0, le=_MAX_ANSWER_TIMEOUT, allow_inf_nan=False
    )
    memory_link_template: str | None = Field(default=None, max_length=_MAX_LINK_TEMPLATE_CHARS)
    entity_extraction_enabled: bool = False
    entity_max: int = Field(default=8, gt=0, le=50)

    @field_validator("memory_link_template")
    @classmethod
    def _memory_link_template_is_safe(cls, value: str | None) -> str | None:
        if value is None:
            return value
        # http(s) only — the rendered link lands in chat messages, so a
        # javascript:/data: scheme must never be storable.
        if not (value.startswith("https://") or value.startswith("http://")):
            raise ValueError("memory_link_template must start with http:// or https://")
        # Exactly the two supported placeholders, plain form only. Attribute
        # or index specs ({context_id.__class__}, {x[0]}) and unknown fields
        # would KeyError (worker-side self-DoS) or become a leak primitive
        # if the worker ever formats with a richer kwargs context.
        try:
            fields = [f for _, f, _, _ in string.Formatter().parse(value) if f is not None]
        except ValueError as e:
            raise ValueError(f"memory_link_template is not a valid template: {e}") from e
        allowed = {"context_id", "memory_id"}
        unsupported = [f for f in fields if f not in allowed]
        if unsupported:
            raise ValueError(
                f"memory_link_template supports only {{context_id}} and {{memory_id}} "
                f"placeholders (got: {sorted(set(unsupported))})"
            )
        missing = sorted(allowed - set(fields))
        if missing:
            raise ValueError(
                "memory_link_template must contain "
                + " and ".join("{" + m + "}" for m in missing)
                + " placeholder(s)"
            )
        return value

    @classmethod
    def from_stored(cls, document: object) -> WorkerRuntimeConfig | None:
        """Leniently rehydrate a stored ``runtime_config`` JSONB document.

        The strict model guards the ADMIN WRITE boundary. Read paths (worker
        vending, connector listing, the update path's previous-value diff)
        must never turn a stored row into an unrecoverable 500: a document
        written by a newer schema (rolling deploy, rollback) would fail
        ``extra="forbid"`` on every read AND on the repair PATCH itself.
        Unknown keys are dropped recursively before validation; if the
        remainder still fails (e.g. a bound tightened across releases), the
        caller gets None — the same "no runtime block, worker defaults"
        contract as a NULL row — instead of an exception.
        """
        if not isinstance(document, dict):
            return None

        def _strip_unknown(data: dict, model: type[BaseModel]) -> dict:
            out: dict = {}
            for key, value in data.items():
                field = model.model_fields.get(key)
                if field is None:
                    continue
                nested = field.annotation
                if (
                    isinstance(value, dict)
                    and isinstance(nested, type)
                    and issubclass(nested, BaseModel)
                ):
                    out[key] = _strip_unknown(value, nested)
                else:
                    out[key] = value
            return out

        try:
            return cls.model_validate(_strip_unknown(document, cls))
        except ValidationError:
            return None
