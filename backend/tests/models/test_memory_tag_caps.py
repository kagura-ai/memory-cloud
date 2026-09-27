"""#1743: remember / update_memory cap the tags of a new write."""

import pytest
from pydantic import ValidationError

from models.schemas import (
    MEMORY_TAG_MAX_CHARS,
    MEMORY_TAGS_MAX_COUNT,
    RememberRequest,
    UpdateMemoryRequest,
)


def _remember(tags):
    return RememberRequest(summary="a summary of at least ten", content="c", type="note", tags=tags)


def test_caps_are_50_tags_of_100_characters():
    assert MEMORY_TAGS_MAX_COUNT == 50
    assert MEMORY_TAG_MAX_CHARS == 100


def test_remember_accepts_tags_at_the_caps():
    tags = [f"{i:02d}" + "x" * (MEMORY_TAG_MAX_CHARS - 2) for i in range(MEMORY_TAGS_MAX_COUNT)]
    assert _remember(tags).tags == tags


@pytest.mark.parametrize(
    "tags",
    [["t"] * (MEMORY_TAGS_MAX_COUNT + 1), ["ok", "x" * (MEMORY_TAG_MAX_CHARS + 1)]],
)
def test_remember_refuses_tags_over_the_caps(tags):
    with pytest.raises(ValidationError):
        _remember(tags)


def test_update_memory_refuses_tags_over_the_caps_and_keeps_none():
    from uuid import uuid4

    assert UpdateMemoryRequest(memory_id=uuid4(), tags=None).tags is None
    with pytest.raises(ValidationError):
        UpdateMemoryRequest(memory_id=uuid4(), tags=["t"] * (MEMORY_TAGS_MAX_COUNT + 1))
    with pytest.raises(ValidationError):
        UpdateMemoryRequest(memory_id=uuid4(), tags=["x" * (MEMORY_TAG_MAX_CHARS + 1)])
