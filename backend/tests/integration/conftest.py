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
# does before collecting test modules. ``api.main`` and ``config.database``
# read ``DATABASE_URL`` at their own import time, so the override must
# happen before they are imported. ``_PREV_DATABASE_URL`` captures the
# original value (if any) so the session-scoped fixture below can restore
# it on exit.
_PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", _DEFAULT_TEST_DB_URL)

# Test-only fallbacks for OAuth/encryption secrets so the integration suite
# runs hermetically when ``.env.local`` was not sourced. ``setdefault`` is
# intentional — when the operator already exports real values (e.g. they
# sourced ``.env.local``), preserve those.
os.environ.setdefault("API_KEY_SECRET", "integration-test-api-key-secret-not-for-prod")
os.environ.setdefault("JWT_SECRET", "integration-test-jwt-secret-not-for-prod")


@pytest.fixture(scope="session", autouse=True)
def _restore_database_url_after_session() -> Generator[None, None, None]:
    """Restore ``DATABASE_URL`` to its prior value at session end.

    The env mutation that paired this fixture happens at conftest *import*
    time (above) because that's when it must take effect; the restoration
    is best-effort cleanup so subsequent invocations / processes that
    inherit the env do not see a leaked override.
    """
    yield
    if _PREV_DATABASE_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV_DATABASE_URL
