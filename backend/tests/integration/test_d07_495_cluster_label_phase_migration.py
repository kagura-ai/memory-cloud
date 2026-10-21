"""Migration round-trip for d07_495_cluster_label_phase (#495, #2079).

1. upgrade widens ``valid_sleep_report_llm_usage_phase`` with
   ``'cluster_labeling'`` and validates it in an autocommit block;
2. downgrade with no such rows restores the previous CHECK fully validated;
3. downgrade with a ``'cluster_labeling'`` usage row present keeps the row
   (cost ledger) and leaves the restored CHECK ``NOT VALID`` instead of failing
   the VALIDATE; the next upgrade validates it again.

The seeded report is deleted at the end (its usage row cascades), so the cost
aggregation tests that share the database do not see it.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

D07_REVISION = "d07_495_cluster_label_phase"
PRIOR = "d06_494_memory_analyses"


def _phase_check(conn) -> str:
    return conn.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'valid_sleep_report_llm_usage_phase'"
        )
    ).scalar_one()


def _phase_check_validated(conn) -> bool:
    return conn.execute(
        text(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conname = 'valid_sleep_report_llm_usage_phase'"
        )
    ).scalar_one()


def test_d07_round_trip_check() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    report_id = str(uuid.uuid4())
    try:
        _upgrade_to(PRIOR)
        with engine.connect() as conn:
            assert "'cluster_labeling'" not in _phase_check(conn)

        _upgrade_to(D07_REVISION)
        with engine.connect() as conn:
            assert "'cluster_labeling'" in _phase_check(conn)
            assert _phase_check_validated(conn) is True

        _downgrade_to(PRIOR)
        with engine.connect() as conn:
            assert "'cluster_labeling'" not in _phase_check(conn)
            assert _phase_check_validated(conn) is True

        _upgrade_to(D07_REVISION)
        with engine.connect() as conn:
            conn.execute(
                text("INSERT INTO sleep_reports (id, user_id) VALUES (:id, 'd07-migration-test')"),
                {"id": report_id},
            )
            conn.execute(
                text(
                    "INSERT INTO sleep_report_llm_usage (report_id, phase, provider, model) "
                    "VALUES (:id, 'cluster_labeling', 'openai', 'gpt-5-nano')"
                ),
                {"id": report_id},
            )
            conn.commit()

        _downgrade_to(PRIOR)
        with engine.connect() as conn:
            kept = conn.execute(
                text("SELECT count(*) FROM sleep_report_llm_usage WHERE report_id = :id"),
                {"id": report_id},
            ).scalar_one()
            assert kept == 1, "downgrade must keep usage rows"
            assert "'cluster_labeling'" not in _phase_check(conn)
            assert _phase_check_validated(conn) is False

        _upgrade_to(D07_REVISION)
        with engine.connect() as conn:
            assert "'cluster_labeling'" in _phase_check(conn)
            assert _phase_check_validated(conn) is True
    finally:
        # Delete first: ``sleep_reports`` exists at every revision this test
        # visits, so the seeded row goes even if the upgrade to head fails.
        try:
            with engine.connect() as conn:
                conn.execute(text("DELETE FROM sleep_reports WHERE id = :id"), {"id": report_id})
                conn.commit()
            _upgrade_to("head")
        finally:
            engine.dispose()
