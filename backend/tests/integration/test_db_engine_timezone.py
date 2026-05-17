"""Regression test for the explicit-UTC engine pinning (#490 scope-B).

Layer 2 of the three-layer UTC policy (see ``.claude/rules/backend.md``)
is the SQLAlchemy engine's ``connect_args`` pinning every session to
``timezone=UTC``. Layers 1 (container env) and 3 (Python writes via
``utcnow()`` + ruff DTZ) each have their own runtime/CI guards; this
test is the guard for layer 2 — if anyone drops ``connect_args`` from
``db.base._get_engine``, this fires.

The test goes through ``db.base._get_engine()`` rather than the test
suite's separate ``async_engine`` fixture so the production code path
itself is exercised.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from db.base import _get_engine, _get_sync_engine


@pytest.mark.asyncio
async def test_async_engine_session_timezone_is_utc() -> None:
    engine = _get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(text("SHOW timezone"))
        assert result.scalar() == "UTC"


def test_sync_engine_session_timezone_is_utc() -> None:
    """OAuth2 sync engine (psycopg2 ``-c timezone=utc``) parity check."""
    with _get_sync_engine().connect() as conn:
        assert conn.execute(text("SHOW timezone")).scalar() == "UTC"
