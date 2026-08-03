"""Validation contract for per-connector ai-worker runtime controls (#1348)."""

import pytest
from pydantic import ValidationError

from models.worker_runtime import WorkerRuntimeConfig, WorkerRuntimeLifecycleConfig


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


class TestReviewHardening:
    """#1350 review: bounds, Infinity, template safety, lenient rehydrate."""

    def test_numeric_knobs_reject_absurd_values(self):
        import pytest as _pytest
        from pydantic import ValidationError as _VE

        from models.worker_runtime import WorkerRuntimeConfig

        for doc in (
            {"buffer": {"max_len": 10**15}},
            {"flush": {"max_tracked_topics": 10**12}},
            {"supervisor": {"tick_seconds": 10**9}},
            {"supervisor": {"tick_seconds": 0.0001}},  # busy-loop floor
            {"supervisor": {"shutdown_flush_timeout_seconds": 1e9}},
            {"answer_timeout_sec": 1e6},
        ):
            with _pytest.raises(_VE):
                WorkerRuntimeConfig.model_validate(doc)

    def test_floats_reject_infinity_and_nan(self):
        import math

        import pytest as _pytest
        from pydantic import ValidationError as _VE

        from models.worker_runtime import WorkerRuntimeConfig

        for value in (math.inf, -math.inf, math.nan):
            with _pytest.raises(_VE):
                WorkerRuntimeConfig.model_validate({"answer_timeout_sec": value})

    def test_link_template_rejects_scheme_and_placeholder_abuse(self):
        import pytest as _pytest
        from pydantic import ValidationError as _VE

        from models.worker_runtime import WorkerRuntimeConfig

        for template in (
            "javascript:alert(1)/{context_id}/{memory_id}",  # scheme injection
            "https://x/{context_id}/{memory_id}/{oops}",  # unknown field → worker KeyError
            "https://x/{context_id.__class__}/{memory_id}",  # attribute access
            "https://x/{context_id}",  # missing memory_id
        ):
            with _pytest.raises(_VE):
                WorkerRuntimeConfig.model_validate({"memory_link_template": template})
        ok = WorkerRuntimeConfig.model_validate(
            {"memory_link_template": "https://x/{context_id}/{memory_id}"}
        )
        assert ok.memory_link_template is not None

    def test_from_stored_drops_unknown_keys_from_newer_schema(self):
        from models.worker_runtime import WorkerRuntimeConfig

        stored = WorkerRuntimeConfig().model_dump(mode="json")
        stored["future_field"] = {"x": 1}  # written by vNext, read after rollback
        stored["buffer"]["future_nested"] = 5
        cfg = WorkerRuntimeConfig.from_stored(stored)
        assert cfg is not None
        assert cfg.buffer.ttl_seconds == 86400

    def test_from_stored_degrades_to_none_instead_of_raising(self):
        from models.worker_runtime import WorkerRuntimeConfig

        # Bound violation that survives key-stripping (tightened across
        # releases): the read path gets the NULL-row contract, not a 500.
        assert WorkerRuntimeConfig.from_stored({"buffer": {"max_len": 10**15}}) is None
        assert WorkerRuntimeConfig.from_stored("not-a-dict") is None
        assert WorkerRuntimeConfig.from_stored(None) is None


class TestNormalizeWorkerLocale:
    """#1377: connector locale must conform to the worker Locale contract (en|ja)."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("en", "en"),
            ("ja", "ja"),
            ("EN", "en"),
            ("Ja", "ja"),
            ("ja-JP", "ja"),
            ("en-US", "en"),
            ("en_GB", "en"),
            (" ja ", "ja"),
            (None, None),
            ("", None),
            ("   ", None),
        ],
    )
    def test_normalizes_conforming_and_bcp47_values(self, raw, expected):
        from models.worker_runtime import normalize_worker_locale

        assert normalize_worker_locale(raw) == expected

    @pytest.mark.parametrize("raw", ["fr", "de-DE", "japanese", "j", "english", "jp"])
    def test_rejects_non_contract_locales(self, raw):
        from models.worker_runtime import normalize_worker_locale

        with pytest.raises(ValueError):
            normalize_worker_locale(raw)


# ── field parity with the consumer's contract ────────────────────────


def test_runtime_config_carries_every_field_the_consumer_can_read():
    """`WorkerRuntimeConfig` is `extra="forbid"`, so a field the consumer supports
    but this model lacks cannot be set AT ALL — the PATCH 422s rather than passing
    it through.

    That is not hypothetical: `edited_summary`, `team_scope_filter_enabled` and
    `channel_allowlist_enabled` shipped on the consumer side and were silently
    unsettable here until this test existed. Each is a dormant capability whose
    whole point is that an operator can flip it per connector.

    Pinned as an explicit list rather than by importing the consumer's model —
    that lives in a different repository, so the contract is asserted here and
    reviewed when it changes.
    """
    expected = {
        "buffer",
        "flush",
        "supervisor",
        "lifecycle",
        "continuity",
        "vision_enabled",
        "team_scope_filter_enabled",
        "channel_allowlist_enabled",
        "mention_answer_enabled",
        "answer_relevance_threshold",
        "answer_timeout_sec",
        "memory_link_template",
        "entity_extraction_enabled",
        "entity_max",
    }
    actual = set(WorkerRuntimeConfig.model_fields)

    assert actual == expected, (
        "runtime contract drifted. Adding a field here is additive and safe; "
        f"only in model={actual - expected}, only in contract={expected - actual}"
    )


def test_lifecycle_block_carries_every_sentinel():
    """Same reasoning, for the nested lifecycle block."""
    expected = {"deletion_mode", "redacted_summary", "dormant_summary", "edited_summary"}

    assert set(WorkerRuntimeLifecycleConfig.model_fields) == expected


def test_the_dormant_flags_default_off():
    """Their defaults are load-bearing: enabling either changes what the consumer
    returns or ingests, so a wrong default here would flip behaviour for every
    connector that has never been configured."""
    cfg = WorkerRuntimeConfig()

    assert cfg.team_scope_filter_enabled is False
    assert cfg.channel_allowlist_enabled is False


def test_the_new_fields_round_trip_through_validation():
    cfg = WorkerRuntimeConfig.model_validate(
        {
            "team_scope_filter_enabled": True,
            "channel_allowlist_enabled": True,
            "lifecycle": {"edited_summary": "[stale]"},
        }
    )

    assert cfg.team_scope_filter_enabled is True
    assert cfg.channel_allowlist_enabled is True
    assert cfg.lifecycle.edited_summary == "[stale]"
