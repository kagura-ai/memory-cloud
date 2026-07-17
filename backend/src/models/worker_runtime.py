"""Validated non-secret runtime controls vended to connector workers (#1348)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WorkerRuntimeBufferConfig(BaseModel):
    """Tenant-owned buffer limits; connection coordinates are intentionally absent."""

    model_config = ConfigDict(extra="forbid")

    ttl_seconds: int = Field(default=86400, gt=0, le=86400)
    max_len: int = Field(default=10_000, gt=0)


class WorkerRuntimeFlushConfig(BaseModel):
    """Per-connector topic flush thresholds."""

    model_config = ConfigDict(extra="forbid")

    silence_seconds: int = Field(default=300, ge=0)
    volume_tokens: int = Field(default=2000, gt=0)
    max_tracked_topics: int = Field(default=10_000, gt=0)


class WorkerRuntimeSupervisorConfig(BaseModel):
    """Per-connector supervisor cadence and shutdown budget."""

    model_config = ConfigDict(extra="forbid")

    tick_seconds: float = Field(default=1.0, gt=0)
    shutdown_flush_timeout_seconds: float = Field(default=25.0, gt=0)


class WorkerRuntimeLifecycleConfig(BaseModel):
    """Connector lifecycle propagation behavior."""

    model_config = ConfigDict(extra="forbid")

    deletion_mode: Literal["forget", "redact"] = "redact"
    redacted_summary: str = Field(default="[redacted on user request]", min_length=1)
    dormant_summary: str = Field(default="[dormant] (channel archived)", min_length=1)


class WorkerRuntimeContinuityConfig(BaseModel):
    """Per-connector topic-continuity thresholds."""

    model_config = ConfigDict(extra="forbid")

    time_window_minutes: int = Field(default=30, ge=0)
    semantic_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
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
    mention_answer_enabled: bool = False
    answer_relevance_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    answer_timeout_sec: float = Field(default=8.0, gt=0.0)
    memory_link_template: str | None = None
    entity_extraction_enabled: bool = False
    entity_max: int = Field(default=8, gt=0, le=50)

    @field_validator("memory_link_template")
    @classmethod
    def _memory_link_template_has_placeholders(cls, value: str | None) -> str | None:
        if value is not None:
            missing = [
                placeholder
                for placeholder in ("{context_id}", "{memory_id}")
                if placeholder not in value
            ]
            if missing:
                raise ValueError(
                    f"memory_link_template must contain {' and '.join(missing)} placeholder(s)"
                )
        return value
