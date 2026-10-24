"""Accepting a supersede suggestion through ``update_memory`` (#2308).

A ``supersede_candidate`` reaches every client, including those on the default
``core`` tool profile, which can reject it (``dismiss_supersede_candidate``)
but had no way to accept it: that took ``create_edge``, which core does not
list. ``accept_supersede_candidate`` is the mirror of the dismissal. These pin
the request contract; the database behaviour is in
``tests/integration/test_2308_accept_supersede_candidate_db.py``.
"""

from uuid import uuid4

import pytest

from models.schemas import UpdateMemoryRequest


class TestAcceptRequestValidation:
    def test_acceptance_defaults_off(self):
        assert UpdateMemoryRequest(memory_id=uuid4()).accept_supersede_candidate is False

    def test_acceptance_is_a_complete_call_on_its_own(self):
        request = UpdateMemoryRequest(memory_id=uuid4(), accept_supersede_candidate=True)
        assert request.accept_supersede_candidate is True

    def test_acceptance_may_carry_expected_version(self):
        """The version guard is not a field edit: it only refuses a stale call."""
        request = UpdateMemoryRequest(
            memory_id=uuid4(), accept_supersede_candidate=True, expected_version=3
        )
        assert request.expected_version == 3

    def test_acceptance_is_rejected_on_the_upsert_path(self):
        with pytest.raises(ValueError, match="accept_supersede_candidate requires memory_id"):
            UpdateMemoryRequest(
                external_id="res-1",
                summary="a summary long enough to pass validation",
                content="content",
                type="note",
                accept_supersede_candidate=True,
            )

    def test_accept_and_dismiss_together_is_refused(self):
        with pytest.raises(ValueError) as exc:
            UpdateMemoryRequest(
                memory_id=uuid4(),
                accept_supersede_candidate=True,
                dismiss_supersede_candidate=True,
            )
        message = str(exc.value)
        assert "accept_supersede_candidate" in message
        assert "dismiss_supersede_candidate" in message

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("summary", "an edited summary long enough"),
            ("context_summary", "edited context"),
            ("content", "edited content"),
            ("details", {"note": "x"}),
            ("type", "fact"),
            ("importance", 0.4),
            ("tags", ["a"]),
            ("context", {"k": "v"}),
            ("delivery_mode", "always"),
        ],
    )
    def test_acceptance_with_a_field_edit_is_refused(self, field, value):
        """Accept is a graph action: an edit in the same call would re-embed and
        version the memory it just marked as superseding — send it separately."""
        with pytest.raises(ValueError) as exc:
            UpdateMemoryRequest(
                memory_id=uuid4(), accept_supersede_candidate=True, **{field: value}
            )
        message = str(exc.value)
        assert "accept_supersede_candidate" in message
        assert field in message

    def test_the_edit_fields_named_by_the_refusal_are_the_service_edit_fields(self):
        """One list decides "is this an edit": the schema refusal and the
        service's dismissal-only check must not drift apart."""
        from models.schemas import UPDATE_MEMORY_EDIT_FIELDS
        from services import memory_service

        assert memory_service.UPDATE_MEMORY_EDIT_FIELDS is UPDATE_MEMORY_EDIT_FIELDS


class TestOwnershipRuleWiring:
    """#1803: the accept applies remember(supersedes=)'s owner rule. Behaviour on
    Postgres (shared, private + linked) is in the integration file; this pins
    that both writes build the target predicate the same way."""

    @staticmethod
    def _function(name):
        import ast
        from pathlib import Path

        import services.memory_service as memory_service

        tree = ast.parse(Path(memory_service.__file__).read_text(encoding="utf-8"))
        return next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name
        )

    @staticmethod
    def _called(fn):
        import ast

        names = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    names.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    names.add(node.func.attr)
        return names

    def test_accept_uses_the_declared_link_owner_predicate(self):
        accept = self._called(self._function("_accept_supersede_candidate"))
        declared = self._called(self._function("_create_declared_links"))
        for helper in ("owned_by", "_is_private_context"):
            assert helper in declared, f"_create_declared_links no longer calls {helper}"
            assert helper in accept, f"the accept no longer applies the #1803 rule ({helper})"
