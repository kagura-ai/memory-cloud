"""Regression test for the explicit-UTC engine pinning (#490 scope-B).

Layer 2 of the three-layer UTC policy (see ``.claude/rules/backend.md``)
is the SQLAlchemy engine's ``connect_args`` pinning every session to
``timezone=UTC``. Layers 1 (container env) and 3 (Python writes via
``utcnow()`` + ruff DTZ) each have their own runtime/CI guards; this
module is the guard for layer 2 — if anyone drops ``connect_args``
from ``db.base._get_engine`` / ``_get_sync_engine``, this fires.

Two complementary checks per engine:

1. **Argument-shape assertion** (the real layer-2 guard): spy on
   ``create_async_engine`` / ``create_engine`` while ``_get_engine()``
   / ``_get_sync_engine()`` runs and verify the exact ``connect_args``
   wired in. This is independent of the postgres server default — if
   somebody drops ``connect_args``, this fails immediately even though
   the container is still ``TZ=UTC``.

2. **End-to-end session check**: open a real connection and read
   ``SHOW timezone``. Catches the rarer case where the
   ``connect_args`` dict is present but malformed (e.g. wrong key
   path), where the spy alone would not notice.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import text

import db.base as db_base


def _reset_cached_engines() -> None:
    """Force ``_get_engine`` / ``_get_sync_engine`` to rebuild on next call.

    The getters are lazy singletons (``db.base.engine`` / ``sync_engine``
    cached after first call). To capture the construction kwargs via a
    spy, we have to dispose any cached engine and clear the module
    globals so the getter falls through to ``create_*_engine`` again.
    """
    for name in ("engine", "sync_engine"):
        existing = getattr(db_base, name, None)
        if existing is not None:
            try:
                if hasattr(existing, "sync_engine") and hasattr(existing.sync_engine, "dispose"):
                    existing.sync_engine.dispose()
                elif hasattr(existing, "dispose"):
                    existing.dispose()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
        if hasattr(db_base, name):
            setattr(db_base, name, None)


@pytest.mark.asyncio
async def test_async_engine_wires_utc_connect_args() -> None:
    """Spy on create_async_engine to assert connect_args is wired correctly.

    Independent of the postgres server default — fires even if the
    container is still ``TZ=UTC``, because the spy reads the kwargs
    SQLAlchemy was called with.
    """
    _reset_cached_engines()
    with patch.object(db_base, "create_async_engine", wraps=db_base.create_async_engine) as spy:
        engine = db_base._get_engine()
        assert spy.call_count == 1
        kwargs = spy.call_args.kwargs
        server_settings = kwargs.get("connect_args", {}).get("server_settings", {})
        assert server_settings.get("timezone") == "UTC", (
            f"async engine connect_args.server_settings.timezone must be 'UTC', "
            f"got connect_args={kwargs.get('connect_args')!r}"
        )
    # End-to-end check: confirm the session actually sees UTC.
    async with engine.connect() as conn:
        result = await conn.execute(text("SHOW timezone"))
        assert result.scalar() == "UTC"


def test_sync_engine_wires_utc_connect_args() -> None:
    """OAuth2 sync engine (psycopg2 ``-c timezone=utc``) parity check.

    Same shape as the async test: spy on create_engine, assert the
    libpq ``options`` string pins timezone to UTC, then verify a real
    connection observes the result.
    """
    _reset_cached_engines()
    with patch.object(db_base, "create_engine", wraps=db_base.create_engine) as spy:
        engine = db_base._get_sync_engine()
        assert spy.call_count == 1
        kwargs = spy.call_args.kwargs
        options = kwargs.get("connect_args", {}).get("options", "")
        # psycopg2 passes libpq options as "-c key=value"; check the
        # whole token rather than substring matching to catch typos.
        assert "-c timezone=utc" in options, (
            f"sync engine connect_args.options must contain '-c timezone=utc', "
            f"got connect_args={kwargs.get('connect_args')!r}"
        )
    with engine.connect() as conn:
        assert conn.execute(text("SHOW timezone")).scalar() == "UTC"
