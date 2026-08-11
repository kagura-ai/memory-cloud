"""Rejection path for supersede suggestions (#1504).

The feature is only worth anything if the suppression actually STICKS. The
detector runs on every re-embed — including sleep reindexes and backfills that
recompute an identical vector — so the tests below are organised around the two
ways this could fail:

* it resurfaces anyway (the dismissal was pointless), or
* it suppresses forever, including a pairing that genuinely became a supersede
  after the memory was rewritten (the dismissal was a trap).
"""

import ast
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from models.schemas import UpdateMemoryRequest
from services.supersede_dismissal import (
    RESURFACE_SIMILARITY_DELTA,
    build_tombstone,
    dismissed_entry,
    is_dismissed,
)

TARGET = str(uuid4())
OTHER = str(uuid4())


def _live_candidate(target=TARGET, similarity=0.9037):
    return {"memory_id": target, "similarity": similarity, "detected_at": "2026-08-08T00:00:00Z"}


class TestTombstoneShape:
    def test_tombstone_is_invisible_to_the_read_path(self):
        """``_resolve_supersede_candidates`` requires a top-level memory_id AND
        similarity; a tombstone must satisfy neither, so it stops surfacing
        without that code needing to know tombstones exist."""
        tombstone = build_tombstone(_live_candidate())
        assert "memory_id" not in tombstone
        assert "similarity" not in tombstone

    def test_tombstone_records_the_pair_and_the_similarity(self):
        tombstone = build_tombstone(_live_candidate(similarity=0.91))
        entry = dismissed_entry(tombstone)
        assert entry is not None
        assert entry["memory_id"] == TARGET
        assert entry["similarity"] == pytest.approx(0.91)
        assert entry["dismissed_at"]

    def test_a_candidate_without_similarity_still_tombstones(self):
        entry = dismissed_entry(build_tombstone({"memory_id": TARGET}))
        assert entry is not None
        assert "similarity" not in entry

    @pytest.mark.parametrize("stored", [None, {}, {"memory_id": TARGET}, "junk", 7])
    def test_non_tombstones_are_not_read_as_dismissals(self, stored):
        assert dismissed_entry(stored) is None


class TestSuppressionSticks:
    def test_the_same_pair_at_the_same_similarity_stays_suppressed(self):
        """A sleep reindex / backfill recomputes an identical vector. If ANY
        re-embed cleared the tombstone, every dismissal would die the first
        night — which is exactly what this keys on the delta to avoid."""
        stored = build_tombstone(_live_candidate(similarity=0.9037))
        assert is_dismissed(stored, target_id=TARGET, similarity=0.9037)

    def test_a_sub_threshold_drift_stays_suppressed(self):
        stored = build_tombstone(_live_candidate(similarity=0.90))
        drift = RESURFACE_SIMILARITY_DELTA / 2
        assert is_dismissed(stored, target_id=TARGET, similarity=0.90 + drift)
        assert is_dismissed(stored, target_id=TARGET, similarity=0.90 - drift)

    def test_a_tombstone_without_a_baseline_stays_suppressed(self):
        """A deliberate judgement with no recorded baseline reads as 'still
        rejected', not 'resurface'."""
        stored = build_tombstone({"memory_id": TARGET})
        assert is_dismissed(stored, target_id=TARGET, similarity=0.99)


class TestSuppressionIsNotATrap:
    def test_a_material_similarity_change_resurfaces(self):
        """The memory was really rewritten — it deserves a fresh judgement."""
        stored = build_tombstone(_live_candidate(similarity=0.90))
        assert not is_dismissed(
            stored, target_id=TARGET, similarity=0.90 + RESURFACE_SIMILARITY_DELTA
        )

    def test_a_dismissal_never_suppresses_a_different_pairing(self):
        """Rejecting one pairing says nothing about another.

        The new candidate is given the SAME similarity as the dismissed one on
        purpose: with a distant score the delta check would mask a missing
        target comparison, and the test would pass on broken code.
        """
        stored = build_tombstone(_live_candidate(target=TARGET, similarity=0.9037))
        assert not is_dismissed(stored, target_id=OTHER, similarity=0.9037)

    def test_a_different_pairing_is_live_even_with_no_recorded_baseline(self):
        """The no-baseline branch must not become a blanket suppressor either."""
        stored = build_tombstone({"memory_id": TARGET})
        assert not is_dismissed(stored, target_id=OTHER, similarity=0.95)

    def test_no_tombstone_suppresses_nothing(self):
        assert not is_dismissed(None, target_id=TARGET, similarity=0.99)
        assert not is_dismissed(_live_candidate(), target_id=TARGET, similarity=0.99)


class TestRequestValidation:
    def test_dismissal_defaults_off(self):
        assert UpdateMemoryRequest(memory_id=uuid4()).dismiss_supersede_candidate is False

    def test_dismissal_is_accepted_in_place_with_no_other_field(self):
        """Rejecting a suggestion is a complete action on its own."""
        request = UpdateMemoryRequest(memory_id=uuid4(), dismiss_supersede_candidate=True)
        assert request.dismiss_supersede_candidate is True

    def test_dismissal_is_rejected_on_the_upsert_path(self):
        """An upsert replaces the memory, so there is no suggestion to dismiss —
        reject rather than silently ignore a caller's mistaken model."""
        with pytest.raises(ValueError, match="requires memory_id"):
            UpdateMemoryRequest(
                external_id="res-1",
                summary="a summary long enough to pass validation",
                content="content",
                type="note",
                dismiss_supersede_candidate=True,
            )


class TestServiceApplication:
    def _memory(self, stored):
        memory = MagicMock()
        memory.id = uuid4()
        memory.supersede_candidate = stored
        return memory

    def _apply(self, stored, *, dismiss=True):
        from services.memory_service import MemoryService

        memory = self._memory(stored)
        request = UpdateMemoryRequest(memory_id=memory.id, dismiss_supersede_candidate=dismiss)
        returned = MemoryService._apply_supersede_dismissal(memory, request)
        return memory, returned

    def test_a_live_suggestion_is_tombstoned_and_echoed(self):
        memory, returned = self._apply(_live_candidate())
        assert str(returned) == TARGET
        assert dismissed_entry(memory.supersede_candidate) is not None

    def test_the_flag_off_leaves_the_suggestion_untouched(self):
        live = _live_candidate()
        memory, returned = self._apply(live, dismiss=False)
        assert returned is None
        assert memory.supersede_candidate == live

    @pytest.mark.parametrize("stored", [None, {}, {"similarity": 0.9}])
    def test_dismissing_with_nothing_pending_is_a_no_op_not_an_error(self, stored):
        """The suggestion may have self-healed or been accepted between the read
        that showed it and this call; failing the whole update would be hostile."""
        memory, returned = self._apply(stored)
        assert returned is None
        assert memory.supersede_candidate == stored

    def test_dismissing_twice_is_idempotent(self):
        memory, first = self._apply(_live_candidate())
        tombstone = memory.supersede_candidate
        request = UpdateMemoryRequest(memory_id=memory.id, dismiss_supersede_candidate=True)

        from services.memory_service import MemoryService

        second = MemoryService._apply_supersede_dismissal(memory, request)
        assert first is not None
        assert second is None
        assert memory.supersede_candidate == tombstone


class TestWiring:
    """Both halves must be connected: the write, and the detector's guard."""

    def _service_ast(self):
        import services.memory_service as memory_service

        return ast.parse(Path(memory_service.__file__).read_text(encoding="utf-8"))

    def test_update_in_place_applies_the_dismissal(self):
        tree = self._service_ast()
        fn = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_update_in_place"
        )
        assert any(
            isinstance(node, ast.Attribute) and node.attr == "_apply_supersede_dismissal"
            for node in ast.walk(fn)
        ), "_update_in_place no longer applies the dismissal"

    def test_the_detector_consults_the_tombstone(self):
        """Without this, the next re-embed rewrites the suggestion and the
        dismissal silently does nothing beyond the current session.

        The detector is located by what it DOES — assigning
        ``memory.supersede_candidate`` — rather than by name, so moving or
        renaming it keeps the guarantee under test instead of quietly passing.
        """
        tree = self._service_ast()

        def writes_a_candidate(fn):
            return any(
                isinstance(node, ast.Attribute)
                and node.attr == "supersede_candidate"
                and isinstance(node.ctx, ast.Store)
                for node in ast.walk(fn)
            )

        writers = [
            fn
            for fn in ast.walk(tree)
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and writes_a_candidate(fn)
        ]
        # The dismissal itself writes the column too; the detector is the other one.
        detectors = [fn for fn in writers if fn.name != "_apply_supersede_dismissal"]
        assert detectors, "no function assigns memory.supersede_candidate any more"
        for fn in detectors:
            assert any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "is_dismissed"
                for node in ast.walk(fn)
            ), (
                f"{fn.name} writes a supersede candidate without consulting the "
                "dismissal tombstone — a re-embed would resurrect rejected suggestions"
            )

    def test_the_dismissal_check_actually_gates_the_write(self):
        """Computing the guard is not enough — it must be in the condition.

        Dropping ``already_dismissed`` from the ``if`` leaves ``is_dismissed``
        called and its result unused, so the previous test still passes while
        the dismissal silently becomes session-only. This asserts on the branch
        that guards the assignment.
        """
        tree = self._service_ast()

        def assigns_candidate(node):
            return any(
                isinstance(n, ast.Attribute)
                and n.attr == "supersede_candidate"
                and isinstance(n.ctx, ast.Store)
                for n in ast.walk(node)
            )

        guards = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and any(assigns_candidate(stmt) for stmt in node.body)
            and any(
                isinstance(n, ast.Name) and n.id == "already_superseded"
                for n in ast.walk(node.test)
            )
        ]
        assert guards, "could not find the branch guarding the candidate write"
        for guard in guards:
            names = {n.id for n in ast.walk(guard.test) if isinstance(n, ast.Name)}
            assert "already_dismissed" in names, (
                "the candidate write is not gated on already_dismissed — the "
                "tombstone is computed but ignored, so a re-embed resurrects "
                "rejected suggestions"
            )

    def test_update_response_carries_the_dismissed_id(self):
        tree = self._service_ast()
        sites = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "UpdateMemoryResponse"
        ]
        assert any(
            any(kw.arg == "supersede_candidate_dismissed" for kw in call.keywords) for call in sites
        ), "no UpdateMemoryResponse reports the dismissal"


class TestDismissalSurvivesASameCallReEmbed:
    """#1504 review: dismissing AND editing in one call must not lose the dismissal."""

    def test_a_reembedding_call_stores_no_similarity_baseline(self):
        """The re-embed is guaranteed to move the score past the resurface delta.

        Recording the pre-edit baseline would let the detector overwrite the
        tombstone moments after the response reported the dismissal as applied.
        """
        tombstone = build_tombstone(_live_candidate(similarity=0.9037), drop_baseline=True)
        entry = dismissed_entry(tombstone)
        assert entry is not None
        assert entry["memory_id"] == TARGET
        assert "similarity" not in entry

    def test_that_tombstone_suppresses_at_any_recomputed_similarity(self):
        stored = build_tombstone(_live_candidate(similarity=0.9037), drop_baseline=True)
        for recomputed in (0.10, 0.50, 0.9037, 0.99):
            assert is_dismissed(stored, target_id=TARGET, similarity=recomputed)

    def test_it_still_does_not_suppress_a_different_pairing(self):
        stored = build_tombstone(_live_candidate(), drop_baseline=True)
        assert not is_dismissed(stored, target_id=OTHER, similarity=0.9037)

    def test_a_non_reembedding_call_keeps_the_baseline(self):
        entry = dismissed_entry(build_tombstone(_live_candidate(similarity=0.9037)))
        assert entry is not None and entry["similarity"] == pytest.approx(0.9037)

    def test_update_in_place_passes_the_reembed_flag(self):
        """Wiring: the flag must be derived from needs_reembed, not hardcoded."""
        import ast
        from pathlib import Path

        import services.memory_service as memory_service

        tree = ast.parse(Path(memory_service.__file__).read_text(encoding="utf-8"))
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_update_in_place"
        )
        calls = [
            c
            for c in ast.walk(fn)
            if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == "_apply_supersede_dismissal"
        ]
        assert calls, "_update_in_place no longer applies the dismissal"
        for call in calls:
            kw = {k.arg: k.value for k in call.keywords}
            assert isinstance(kw.get("drop_baseline"), ast.Name), (
                "drop_baseline must come from needs_reembed"
            )
            assert kw["drop_baseline"].id == "needs_reembed"


class TestMalformedStoredCandidate:
    """#1504 review: a corrupt ADVISORY column must not fail the caller's edit."""

    def _apply(self, stored):
        from services.memory_service import MemoryService

        memory = MagicMock()
        memory.id = uuid4()
        memory.supersede_candidate = stored
        request = UpdateMemoryRequest(memory_id=memory.id, dismiss_supersede_candidate=True)
        return memory, MemoryService._apply_supersede_dismissal(memory, request)

    @pytest.mark.parametrize("bad", ["not-a-uuid", "", "12345", "  "])
    def test_a_malformed_target_id_is_dropped_not_raised(self, bad):
        memory, returned = self._apply({"memory_id": bad, "similarity": 0.9})
        assert returned is None

    def test_the_row_is_left_untouched_when_the_id_is_malformed(self):
        stored = {"memory_id": "not-a-uuid", "similarity": 0.9}
        memory, _ = self._apply(stored)
        assert memory.supersede_candidate == stored, (
            "a half-applied tombstone would be worse than none"
        )


class TestDismissalIsNotAContentEdit:
    """#1504 review: updated_at is the documented staleness cue."""

    def test_dismissal_only_update_restores_updated_at(self):
        import ast
        from pathlib import Path

        import services.memory_service as memory_service

        tree = ast.parse(Path(memory_service.__file__).read_text(encoding="utf-8"))
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_update_in_place"
        )
        restores = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.If)
            and any(
                isinstance(t, ast.Attribute)
                and t.attr == "updated_at"
                and isinstance(t.ctx, ast.Store)
                for t in ast.walk(n)
            )
        ]
        assert restores, (
            "_update_apply_fields stamps updated_at unconditionally; a "
            "dismissal-only call must put it back"
        )
        names = {n.id for r in restores for n in ast.walk(r.test) if isinstance(n, ast.Name)}
        assert "edits_requested" in names
