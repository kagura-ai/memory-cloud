"""Pytest conftest for the integration test suite.

Steers ``DATABASE_URL`` at ``TEST_DATABASE_URL`` deterministically before
any test module imports ``config.database`` or ``api.main``, so the route
handler's ``get_sync_session()`` connects to the test DB regardless of
which module is collected first.

The mutation happens at **conftest import time** (module top, below the
imports) because ``config.database.DATABASE_URL`` and
``api.main`` read the env at their own import time. A session-scoped
autouse fixture restores the prior value at session end so a leaked
override doesn't follow the user out of pytest.

Mirrors the env-steering pattern in
``tests/integration/test_alembic_migrations.py:_alembic_at_test_db`` —
that one is per-call (context manager) for the migration tests; this
conftest applies the same idea suite-wide.
"""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest

_DEFAULT_TEST_DB_URL = "postgresql+asyncpg://kagura:kagura_dev_password@localhost:5432/kagura_test"

# CRITICAL: this assignment runs at conftest *import* time, which pytest
# does before collecting test modules in this directory. ``api.main`` and
# ``config.database`` read ``DATABASE_URL`` at their own import time, so the
# override must happen before they are imported. ``_PREV_DATABASE_URL``
# captures the original value (if any) so the session-scoped fixture below
# can restore it on exit.
_PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
_PREV_DB_NULL_POOL = os.environ.get("DB_NULL_POOL")
_PREV_SCHEDULER_LEADER_ENABLED = os.environ.get("SCHEDULER_LEADER_ENABLED")
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", _DEFAULT_TEST_DB_URL)

# #1988: this process runs several event loops against one process-global
# engine — pytest-asyncio's session loop for the async tests and one anyio
# portal loop per ``TestClient(app)`` lifespan. asyncpg connections are bound
# to the loop that opened them, so a pooled connection checked in from one
# loop and checked out on another is terminated on checkout (``pool_pre_ping``)
# from the wrong loop, which once stalled a lifespan for the whole
# ``--timeout``. ``DB_NULL_POOL`` gives the engines a ``NullPool`` so no
# connection outlives its checkout; ``SCHEDULER_LEADER_ENABLED=false`` keeps
# each lifespan from holding the leader's dedicated connection for its whole
# life and from running cron jobs inside pytest. ``setdefault`` lets an
# operator override either for a targeted run.
os.environ.setdefault("DB_NULL_POOL", "true")
os.environ.setdefault("SCHEDULER_LEADER_ENABLED", "false")

# Test-only fallbacks for OAuth/encryption secrets so the integration suite
# runs hermetically when ``.env.local`` was not sourced. ``setdefault`` is
# intentional — when the operator already exports real values (e.g. they
# sourced ``.env.local``), preserve those.
os.environ.setdefault("API_KEY_SECRET", "integration-test-api-key-secret-not-for-prod")
os.environ.setdefault("JWT_SECRET", "integration-test-jwt-secret-not-for-prod")

# If a sibling test directory (e.g. ``tests/api/``) was collected before
# ``tests/integration/`` and already imported ``config.database``, the
# module-level ``DATABASE_URL`` constant in that module is frozen at the
# pre-override value. Reload only the configuration module so it re-reads
# the env we just set. For ``db.base``, do NOT reload the module because
# that would recreate ``Base = declarative_base()`` and split the
# declarative metadata registry from the model modules that already
# imported the original ``Base`` (alembic ``target_metadata`` and any
# ``Base.metadata.create_all`` would then see only an empty registry).
# Instead, reset the lazy-init globals (``engine``, ``async_session_factory``,
# ``sync_engine``, ``sync_session_factory``) in place so the next call
# to ``_get_engine()`` / ``_get_sync_engine()`` rebuilds them against the
# freshly reloaded ``config.database.DATABASE_URL`` (those getters do
# ``from config.database import DATABASE_URL`` at call time, so the
# new value is picked up automatically).
#
# This is defensive — when integration tests are run via
# ``make test-integration`` (which does NOT mix with unit tests), no
# reload/reset is needed; the guard only fires in mixed-suite invocations
# (``pytest tests/``).
import importlib  # noqa: E402
import sys  # noqa: E402


def _reset_db_base_state() -> None:
    """Clear cached engines/session factories on ``db.base`` without reloading.

    ``db.base`` lazily creates engines on first access via the
    ``from config.database import DATABASE_URL`` pattern inside its getter
    functions. Setting the cached globals to ``None`` (and disposing the
    engines first to release pooled connections) makes the next access
    rebuild them with the freshly reloaded config — without touching the
    declarative ``Base`` that model modules already hold a reference to.
    """
    db_base = sys.modules.get("db.base")
    if db_base is None:
        return

    for engine_name in ("engine", "sync_engine"):
        existing = getattr(db_base, engine_name, None)
        if existing is not None:
            # ``db.base.engine`` is an ``AsyncEngine`` whose ``.dispose()``
            # returns a coroutine — calling it without ``await`` here would
            # leak an un-awaited coroutine and never actually release the
            # pool. Dispose via the underlying ``sync_engine`` attribute
            # for async engines (``AsyncEngine.sync_engine.dispose()`` is
            # synchronous and releases the same pool); fall back to plain
            # ``.dispose()`` for the sync engine in ``db.base.sync_engine``.
            try:
                if hasattr(existing, "sync_engine") and hasattr(existing.sync_engine, "dispose"):
                    existing.sync_engine.dispose()
                elif hasattr(existing, "dispose"):
                    existing.dispose()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
        if hasattr(db_base, engine_name):
            setattr(db_base, engine_name, None)

    for factory_name in ("async_session_factory", "sync_session_factory"):
        if hasattr(db_base, factory_name):
            setattr(db_base, factory_name, None)


if "config.database" in sys.modules:
    importlib.reload(sys.modules["config.database"])

# ``get_settings()`` caches one ``Settings`` per process; a sibling directory
# collected first may already have built it without the env set above. Set
# the two fields on that instance rather than discarding it: ``api.main``
# binds ``settings = get_settings()`` at import, and a second instance would
# split the singleton for tests that patch it.
_cached_settings = getattr(sys.modules.get("config.settings"), "_settings", None)
if _cached_settings is not None:
    _cached_settings.db_null_pool = os.environ["DB_NULL_POOL"].lower() in ("1", "true", "yes")
    _cached_settings.scheduler_leader_enabled = os.environ["SCHEDULER_LEADER_ENABLED"].lower() in (
        "1",
        "true",
        "yes",
    )

_reset_db_base_state()


@pytest.fixture(scope="session", autouse=True)
def _restore_database_url_after_session() -> Generator[None, None, None]:
    """Restore ``DATABASE_URL`` to its prior value at session end.

    The env mutation that paired this fixture happens at conftest *import*
    time (above) because that's when it must take effect; the restoration
    is best-effort cleanup so subsequent invocations / processes that
    inherit the env do not see a leaked override.
    """
    yield
    for name, prev in (
        ("DB_NULL_POOL", _PREV_DB_NULL_POOL),
        ("SCHEDULER_LEADER_ENABLED", _PREV_SCHEDULER_LEADER_ENABLED),
    ):
        if prev is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prev
    if _PREV_DATABASE_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV_DATABASE_URL
