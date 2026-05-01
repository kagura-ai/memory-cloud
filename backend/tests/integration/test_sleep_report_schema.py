"""Integration tests for the source/paid_by schema additions to sleep_reports (#523).

Covers:

- CHECK constraint on ``source`` rejects values outside ``('sleep', 'analysis')``.
- CHECK constraint on ``paid_by`` rejects values outside ``('platform', 'byok')``.
- ORM Python-side default: a row constructed without ``source``/``paid_by``
  has them set on the in-memory object before flush (no refresh needed).
- ``server_default``: ``information_schema.columns.column_default`` confirms
  both columns carry the expected DEFAULT clause in the catalog. Guards
  against a malformed expression (e.g. ``text("sleep")`` without inner
  quotes) that ORM-only tests would silently mask.
- BYOK round-trip: ``source='analysis'`` + ``paid_by='byok'`` persists and
  reads back unchanged. This is the path #495 broadlistening pipeline writes.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from models.sleep import SleepReport


def _make_report(**overrides: object) -> SleepReport:
    """Minimal SleepReport constructor; callers override columns under test.

    No ``source``/``paid_by`` defaults injected — those are filled by the
    ORM column ``default=`` so this helper exercises the production path.
    """
    defaults: dict[str, object] = {
        "user_id": "test-user",
        "status": "completed",
    }
    defaults.update(overrides)
    return SleepReport(**defaults)


@pytest.mark.asyncio
async def test_check_rejects_invalid_source(db_session):
    """source outside ('sleep', 'analysis') must raise IntegrityError."""
    db_session.add(_make_report(source="manual"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_check_rejects_invalid_paid_by(db_session):
    """paid_by outside ('platform', 'byok') must raise IntegrityError."""
    db_session.add(_make_report(paid_by="user"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_orm_defaults_populate_after_flush_without_refresh(db_session):
    """A SleepReport built without source/paid_by persists with the Python defaults.

    Verifies the Python-side ``default=`` fires during ``flush()`` and the
    resulting value is visible on the in-memory object without a separate
    ``refresh()`` round-trip. (SQLAlchemy applies scalar ``default=`` at
    INSERT time, not at ``__init__``; the guarantee here is "no refresh
    needed", not "no flush needed".)
    """
    report = SleepReport(user_id="test-user", status="completed")
    db_session.add(report)
    await db_session.flush()

    assert report.source == "sleep"
    assert report.paid_by == "platform"


@pytest.mark.asyncio
async def test_server_default_clause_present_in_schema(db_session):
    """``server_default`` is declared in the schema for both new columns.

    Reads ``information_schema.columns.column_default`` directly. Catches a
    malformed ``server_default`` expression (e.g. ``text("sleep")`` without
    inner quotes) where the ORM-derived schema would either fail to create
    or carry the wrong DEFAULT clause — the Python-side ``default=``-only
    tests would mask either failure mode.
    """
    rows = (
        await db_session.execute(
            sa.text(
                "SELECT column_name, column_default "
                "FROM information_schema.columns "
                "WHERE table_name = 'sleep_reports' "
                "AND column_name IN ('source', 'paid_by') "
                "ORDER BY column_name"
            )
        )
    ).all()

    defaults = {r.column_name: r.column_default for r in rows}
    assert "'platform'::character varying" in defaults["paid_by"]
    assert "'sleep'::character varying" in defaults["source"]


@pytest.mark.asyncio
async def test_byok_override_round_trips(db_session):
    """source='analysis' + paid_by='byok' persists and reads back unchanged.

    This is the exact pair of values #495 broadlistening pipeline writes
    in its persist transaction.
    """
    report = _make_report(source="analysis", paid_by="byok")
    db_session.add(report)
    await db_session.flush()
    await db_session.refresh(report)

    assert report.source == "analysis"
    assert report.paid_by == "byok"
