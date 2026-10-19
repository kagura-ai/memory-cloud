"""``RecallTimings`` — the per-stage clock behind ``recall_completed`` (#1989)."""

from __future__ import annotations

from services.recall_timings import RecallTimings


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_stages_are_reported_in_ms_and_unrun_stages_are_omitted() -> None:
    clock = _Clock()
    t = RecallTimings(clock=clock)
    with t.stage("embed"):
        clock.advance(0.012)
    with t.stage("qdrant"):
        clock.advance(0.040)
    with t.stage("hydrate"):
        clock.advance(0.0031)
    clock.advance(0.010)  # unmeasured work between stages

    fields = t.log_fields(results=3, k=5)

    assert fields["embed_ms"] == 12
    assert fields["qdrant_ms"] == 40
    assert fields["hydrate_ms"] == 3
    assert "rerank_ms" not in fields
    assert "fulltext_ms" not in fields
    assert "post_commit_ms" not in fields
    assert fields["duration_ms"] == 65
    assert fields["other_ms"] == 10
    assert fields["results"] == 3 and fields["k"] == 5
    assert "embed_cache_hit" not in fields


def test_a_stage_entered_twice_accumulates() -> None:
    """finalize runs in two spans (response build, then hints/confidence)."""
    clock = _Clock()
    t = RecallTimings(clock=clock)
    with t.stage("finalize"):
        clock.advance(0.004)
    with t.stage("finalize"):
        clock.advance(0.006)
    assert t.log_fields()["finalize_ms"] == 10


def test_a_failing_stage_is_still_timed() -> None:
    clock = _Clock()
    t = RecallTimings(clock=clock)
    try:
        with t.stage("embed"):
            clock.advance(0.5)
            raise RuntimeError("provider down")
    except RuntimeError:
        pass
    assert t.log_fields()["embed_ms"] == 500


def test_embed_cache_hit_is_reported_when_known() -> None:
    t = RecallTimings(clock=_Clock())
    t.embed_cache_hit = True
    assert t.log_fields()["embed_cache_hit"] is True


def test_field_order_is_stable_for_log_readers() -> None:
    clock = _Clock()
    t = RecallTimings(clock=clock)
    for name in (
        "finalize",
        "commit",
        "hydrate",
        "rerank",
        "fulltext",
        "qdrant",
        "embed",
        "post_commit",
        "select",
    ):
        with t.stage(name):
            clock.advance(0.001)
    keys = [k for k in t.log_fields() if k.endswith("_ms")]
    assert keys == [
        "duration_ms",
        "embed_ms",
        "qdrant_ms",
        "fulltext_ms",
        "rerank_ms",
        "hydrate_ms",
        "select_ms",
        "finalize_ms",
        "commit_ms",
        "post_commit_ms",
        "other_ms",
    ]
