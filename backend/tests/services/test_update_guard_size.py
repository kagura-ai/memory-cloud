"""Regression tests for ``MemoryService._update_guard_size`` (#1458).

The guard sized the post-update row with truthy ``or`` fallbacks. On this
surface ``None`` means "leave this field alone", but ``""`` and ``{}`` are
values a client can legitimately send — ``context_summary`` has no
``min_length`` and ``details`` is a free dict — and ``_update_apply_fields``
writes them. Being falsy, they fell through to the STORED value, so an update
that CLEARED a large field was measured as though it had not, and could be
rejected with ``QuotaExceededError`` while actually shrinking the row.

``summary`` (``min_length=10``) and ``content`` (``min_length=1``) reject ``""``
at the schema, so only ``context_summary`` and ``details`` were reachable.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from config.constants import MAX_CONTENT_SIZE
from models.schemas import UpdateMemoryRequest
from services.memory_service import MemoryService
from utils.exceptions import QuotaExceededError


def _guard(memory, request):
    """Call the guard the way ``update_memory`` does.

    The caller pre-normalizes the two summary fields and hands them in, so the
    measurement matches what lands on the row. ``normalize_for_search`` is
    identity for the ASCII used here.
    """
    MemoryService._update_guard_size(
        memory,
        request,
        request.summary,
        request.context_summary,
    )


def _stored(*, summary="stored summary", context_summary="", content="", details=None):
    return SimpleNamespace(
        summary=summary,
        context_summary=context_summary,
        content=content,
        details=details,
    )


def test_clearing_a_large_context_summary_is_not_rejected():
    """#1458 core case: the update SHRINKS the row, so it must be allowed.

    The stored row sits just under the limit only because of its
    context_summary; clearing it takes the row far below. The old guard read
    the stored value back (``"" or stored``) and rejected the update.
    """
    stored = _stored(
        context_summary="c" * 2000,
        content="x" * (MAX_CONTENT_SIZE - 1000),
    )
    request = UpdateMemoryRequest(memory_id=uuid4(), context_summary="")

    _guard(stored, request)  # must not raise


def test_replacing_large_details_with_an_empty_dict_is_not_rejected():
    """Same defect through the other reachable field.

    The stored ``details`` alone exceed the limit, so falling back to them (the
    pre-fix behaviour) rejects the update; the post-update row is 402 bytes.
    """
    stored = _stored(
        details={"blob": "d" * MAX_CONTENT_SIZE},
        content="x" * 400,
    )
    request = UpdateMemoryRequest(memory_id=uuid4(), details={})

    _guard(stored, request)  # must not raise


def test_empty_details_counts_as_its_own_two_bytes_not_zero():
    """``{}`` is a value, not an absence: ``len(str({})) == 2``.

    Pins the distinction the fix rests on — the sibling ``_patch_guard_size``
    documents the same rule.
    """
    stored = _stored(details={"blob": "d" * 100}, content="x" * MAX_CONTENT_SIZE)
    request = UpdateMemoryRequest(memory_id=uuid4(), details={})

    # content alone is exactly at the limit; the two bytes of "{}" push it over.
    with pytest.raises(QuotaExceededError):
        _guard(stored, request)


def test_a_genuinely_oversized_update_is_still_rejected():
    """The guard must still do its job — the fix is not a loosening."""
    stored = _stored(content="x" * 100)
    request = UpdateMemoryRequest(
        memory_id=uuid4(),
        content="y" * (MAX_CONTENT_SIZE + 1),
    )

    with pytest.raises(QuotaExceededError) as exc:
        _guard(stored, request)
    assert "exceeds limit" in str(exc.value)


def test_omitted_fields_are_sized_from_the_stored_row():
    """``None`` still means "leave alone" — that semantic is unchanged.

    An importance-only update on an already-at-limit row must still be measured
    against the stored content, or the guard would stop guarding.
    """
    stored = _stored(content="x" * (MAX_CONTENT_SIZE + 1))
    request = UpdateMemoryRequest(memory_id=uuid4(), importance=0.9)

    with pytest.raises(QuotaExceededError):
        _guard(stored, request)


def test_a_none_stored_field_does_not_crash_the_measurement():
    """Legacy rows carry NULL content / details; ``len(None)`` would raise."""
    stored = _stored(summary="s" * 10, context_summary=None, content=None, details=None)
    request = UpdateMemoryRequest(memory_id=uuid4(), importance=0.5)

    _guard(stored, request)  # must not raise
