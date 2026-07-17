"""Validation contract for per-connector ai-worker runtime controls (#1348)."""

import pytest
from pydantic import ValidationError

from models.worker_runtime import WorkerRuntimeConfig


def test_worker_runtime_config_materializes_worker_compatible_defaults() -> None:
    runtime = WorkerRuntimeConfig.model_validate({"vision_enabled": False})

    assert runtime.vision_enabled is False
    assert runtime.buffer.ttl_seconds == 86400
    assert runtime.buffer.max_len == 10_000
    assert runtime.flush.silence_seconds == 300
    assert runtime.supervisor.shutdown_flush_timeout_seconds == 25.0
    assert runtime.lifecycle.deletion_mode == "redact"
    assert runtime.continuity.semantic_check_enabled is False
    assert runtime.mention_answer_enabled is False
    assert runtime.entity_extraction_enabled is False


@pytest.mark.parametrize(
    "payload",
    [
        {"unknown_control": True},
        {"buffer": {"redis_url": "redis://tenant.invalid:6379/0"}},
        {"buffer": {"ttl_seconds": 86401}},
        {"answer_relevance_threshold": 1.1},
        {"entity_max": 51},
        {"memory_link_template": "https://memory.example/{memory_id}"},
    ],
)
def test_worker_runtime_config_rejects_unknown_or_invalid_controls(payload: object) -> None:
    with pytest.raises(ValidationError):
        WorkerRuntimeConfig.model_validate(payload)


def test_worker_runtime_config_normalizes_nested_values_for_jsonb() -> None:
    runtime = WorkerRuntimeConfig.model_validate(
        {
            "buffer": {"ttl_seconds": 3600, "max_len": 250},
            "vision_enabled": False,
            "memory_link_template": (
                "https://memory.example/contexts/{context_id}?memoryId={memory_id}"
            ),
        }
    )

    stored = runtime.model_dump(mode="json")

    assert stored["buffer"] == {"ttl_seconds": 3600, "max_len": 250}
    assert stored["vision_enabled"] is False
    assert stored["flush"]["max_tracked_topics"] == 10_000
