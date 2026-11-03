"""Accepting a supersede suggestion through ``update_memory`` (#2308).

A ``supersede_candidate`` reaches every client, including those on the default
``core`` tool profile, which can reject it (``dismiss_supersede_candidate``)
but had no way to accept it: that took ``create_edge``, which core does not
list. ``accept_supersede_candidate`` is the mirror of the dismissal. These pin
the request contract; the database behaviour is in
``tests/integration/test_2308_accept_supersede_candidate_db.py``. #2600 adds
the same two flags to ``PATCH /api/v1/memory/{id}`` (the web dialog's accept,
``tests/integration/test_2600_patch_accept_supersede_db.py``).
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


class TestPatchAcceptRequestValidation:
    """#2600: ``PATCH /api/v1/memory/{id}`` takes both flags with update_memory's
    rules, so the web dialog's accept runs the same path. PATCH's field
    presence is ``model_fields_set``: ``{"details": null}`` is an edit there."""

    @staticmethod
    def _patch(**body):
        from models.schemas import PatchMemoryRequest

        return PatchMemoryRequest.model_validate(body)

    def test_both_flags_default_off(self):
        request = self._patch(importance=0.5)
        assert request.accept_supersede_candidate is False
        assert request.dismiss_supersede_candidate is False

    @pytest.mark.parametrize("flag", ["accept_supersede_candidate", "dismiss_supersede_candidate"])
    def test_either_flag_alone_is_a_complete_patch(self, flag):
        assert getattr(self._patch(**{flag: True}), flag) is True

    @pytest.mark.parametrize("flag", ["accept_supersede_candidate", "dismiss_supersede_candidate"])
    def test_either_flag_may_carry_expected_version(self, flag):
        """As on update_memory: the version guard only refuses a stale call."""
        assert self._patch(**{flag: True}, expected_version=3).expected_version == 3

    @pytest.mark.parametrize("flag", ["accept_supersede_candidate", "dismiss_supersede_candidate"])
    def test_a_false_flag_alone_is_an_empty_patch(self, flag):
        with pytest.raises(ValueError, match="at least one field"):
            self._patch(**{flag: False})

    @pytest.mark.parametrize("flag", ["accept_supersede_candidate", "dismiss_supersede_candidate"])
    @pytest.mark.parametrize("value", ["true", 1, None])
    def test_a_flag_is_a_json_boolean(self, flag, value):
        """Fail closed like the MCP tool's accept: a string or a number is refused."""
        with pytest.raises(ValueError):
            self._patch(**{flag: value})

    def test_accept_and_dismiss_together_is_refused(self):
        with pytest.raises(ValueError) as exc:
            self._patch(accept_supersede_candidate=True, dismiss_supersede_candidate=True)
        message = str(exc.value)
        assert "accept_supersede_candidate" in message
        assert "dismiss_supersede_candidate" in message

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("summary", "an edited summary long enough"),
            ("content", "edited content"),
            ("type", "fact"),
            ("importance", 0.4),
            ("tags", ["a"]),
            ("add_tags", ["a"]),
            ("remove_tags", ["a"]),
            ("details", {"note": "x"}),
            ("details", None),
            ("details_patch", {"note": "x"}),
            ("observed_at", "2026-11-01"),
        ],
    )
    def test_accept_with_a_field_edit_is_refused(self, field, value):
        with pytest.raises(ValueError) as exc:
            self._patch(accept_supersede_candidate=True, **{field: value})
        message = str(exc.value)
        assert "accept_supersede_candidate" in message
        assert field in message

    def test_accept_with_a_null_observed_at_is_not_an_edit(self):
        """``observed_at: null`` means unchanged on PATCH, as in the empty-patch rule."""
        request = self._patch(accept_supersede_candidate=True, observed_at=None)
        assert request.accept_supersede_candidate is True

    def test_a_dismissal_may_share_the_patch_with_an_edit(self):
        """As on update_memory: rejecting the suggestion while editing the memory."""
        request = self._patch(dismiss_supersede_candidate=True, importance=0.4)
        assert request.dismiss_supersede_candidate is True
        assert request.importance == 0.4


class TestOwnMemoryPredicate:
    """#1803: the one owner rule remember(supersedes=) and the accept share.
    Behaviour on Postgres (shared, private + linked) is in the integration file."""

    @staticmethod
    async def _predicate(private: bool | None) -> str:
        from unittest.mock import AsyncMock, MagicMock

        from services.memory_service import MemoryService

        service = MemoryService.__new__(MemoryService)
        service.db = MagicMock()
        service.db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=private))
        )
        return str(await service._own_memory_predicate(uuid4(), "u1"))

    @pytest.mark.asyncio
    async def test_a_shared_context_keeps_the_callers_own_memories(self):
        sql = await self._predicate(False)
        assert "memories.user_id = " in sql
        assert "identity_links" not in sql

    @pytest.mark.asyncio
    async def test_a_private_context_widens_to_the_link_set(self):
        sql = await self._predicate(True)
        assert "identity_links" in sql

    @pytest.mark.asyncio
    async def test_an_unknown_context_keeps_the_callers_own_memories(self):
        sql = await self._predicate(None)
        assert "memories.user_id = " in sql
        assert "identity_links" not in sql
