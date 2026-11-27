"""The exempt tags (#3022): one reader of ``SLEEP_ARCHIVE_EXEMPT_TAGS`` for new code.

A memory carrying one of these tags (exact match, default ``status:open``) is
kept off the never-delivered review list and, in Sleep, off the losing side of
a near-duplicate merge. The value is the process environment's: the API
service and the Sleep worker each read their own, so an operator sets it for
both.

``exempt_tags()`` reads the variable on every call (a changed value takes
effect at once) and parses each distinct raw value once, so a malformed value
logs its warning once per process instead of once per request.
"""

from __future__ import annotations

import os
from collections.abc import Collection, Iterable
from functools import lru_cache

from sqlalchemy import cast, not_, or_, true
from sqlalchemy.sql import ColumnElement

from models.memory import Memory
from neural.config import sleep_archive_exempt_tags_from_env

_ENV = "SLEEP_ARCHIVE_EXEMPT_TAGS"


@lru_cache(maxsize=8)
def _parse(raw: str | None) -> frozenset[str]:
    """Parse one raw value with the Sleep parser (it reads the variable itself).

    ``raw`` is the cache key: the caller read it a moment before, and the
    parser reads the same environment.
    """
    del raw
    return frozenset(sleep_archive_exempt_tags_from_env())


def exempt_tags() -> frozenset[str]:
    """The exempt tags of this process; empty when the setting turns them off."""
    return _parse(os.getenv(_ENV))


def carries_exempt_tag(tags: Iterable[str] | None, exempt: Collection[str]) -> bool:
    """Whether ``tags`` holds one of ``exempt`` (exact match; ``None`` holds none)."""
    if not exempt or not tags:
        return False
    return any(tag in exempt for tag in tags)


def not_exempt_tagged_predicate(exempt: Collection[str]) -> ColumnElement[bool]:
    """SQL: the row carries none of ``exempt``. NULL-safe; ``true()`` when ``exempt`` is empty.

    The tags are bound sorted, so the statement text does not depend on the
    set's iteration order.
    """
    if not exempt:
        return true()
    arr = cast(sorted(exempt), Memory.tags.type)
    return or_(Memory.tags.is_(None), not_(Memory.tags.overlap(arr)))
