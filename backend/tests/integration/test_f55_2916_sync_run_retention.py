"""#2916 (f55): the change count is backfilled from the stored diffs, ended runs
drop theirs, the sweep indexes are built, and downgrade keeps every row."""

import json
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import inspect, text

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)
from tests.integration.test_f50_2807_merge_sync_heads import (
    _assert_preserved,
    _seed_common,
    _seed_sync,
)

BEFORE = "f54_2882_ordered_billing"
REVISION = "f55_2916_sync_run_retention"
INDEXES = {
    "sync_runs": {"ix_sync_runs_state_expires_at", "ix_sync_runs_committed_diff"},
    "sync_index_outbox": {"ix_sync_index_outbox_undelivered"},
}


def _diff(size: int) -> list[dict]:
    return [
        {"external_id": f"e{n}", "op": "upsert", "before_hash": "a", "after_hash": "b"}
        for n in range(size)
    ]


def _seed_diffs(engine, ids) -> dict[str, object]:
    """Runs in every state, written as the f54 schema stores them (raw SQL:
    the current model also writes ``change_count``)."""
    runs = {
        "aborted": ("aborted", _diff(3)),
        "expired": ("expired", _diff(2)),
        "prepared": ("prepared", _diff(4)),
        "unprepared": ("uploading", None),
    }
    keys = {}
    with engine.begin() as conn:
        for name, (state, diff) in runs.items():
            keys[name] = uuid4()
            conn.execute(
                text(
                    "INSERT INTO sync_runs (id, feed_id, operation_id, request_hash, manifest, "
                    "generations, base_revision, state, diff, requires_approval, created_at, "
                    "expires_at) VALUES (:id, :feed, :op, :hash, '{}', '[1, 1, 1]', 0, :state, "
                    "CAST(:diff AS jsonb), false, :now, :expires)"
                ),
                {
                    "id": keys[name],
                    "feed": ids.fid,
                    "op": uuid4(),
                    "hash": "2" * 64,
                    "state": state,
                    "diff": None if diff is None else json.dumps(diff),
                    "now": ids.now,
                    "expires": ids.now + timedelta(hours=24),
                },
            )
        conn.execute(
            text("UPDATE sync_runs SET diff = CAST(:diff AS jsonb) WHERE id = :id"),
            {"diff": json.dumps(_diff(1)), "id": ids.committed},
        )
    keys["committed"] = ids.committed
    return keys


def _runs(engine, keys) -> dict[str, tuple]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, state, change_count, jsonb_array_length(diff) FROM sync_runs "
                "WHERE id = ANY(:ids)"
            ),
            {"ids": list(keys.values())},
        ).all()
    by_id = {row[0]: row[1:] for row in rows}
    return {name: by_id[key] for name, key in keys.items()}


def _indexes(engine) -> dict[str, set[str]]:
    found = inspect(engine)
    return {
        table: {index["name"] for index in found.get_indexes(table)} & names
        for table, names in INDEXES.items()
    }


def test_change_count_backfill_clears_ended_diffs_and_round_trips():
    engine = _sync_engine()
    try:
        _reset_alembic_state()
        _upgrade_to(BEFORE)
        ids = _seed_common(engine)
        _seed_sync(engine, ids)
        keys = _seed_diffs(engine, ids)
        _upgrade_to(REVISION)
        assert _runs(engine, keys) == {
            "aborted": ("aborted", 3, None),
            "expired": ("expired", 2, None),
            "prepared": ("prepared", 4, 4),
            "unprepared": ("uploading", None, None),
            "committed": ("committed", 1, 1),
        }
        assert _indexes(engine) == INDEXES
        _assert_preserved(engine, ids, sync=True, budget=False)
        # Downgrade drops the column and the indexes, never a row.
        _downgrade_to(BEFORE)
        assert _indexes(engine) == {table: set() for table in INDEXES}
        assert "change_count" not in {c["name"] for c in inspect(engine).get_columns("sync_runs")}
        _assert_preserved(engine, ids, sync=True, budget=False)
        with engine.connect() as conn:
            assert conn.scalar(
                text("SELECT count(*) FROM sync_runs WHERE id = ANY(:ids)"),
                {"ids": list(keys.values())},
            ) == len(keys)
        # Upgrading again counts the diffs still stored.
        _upgrade_to(REVISION)
        assert _indexes(engine) == INDEXES
        assert _runs(engine, keys)["prepared"] == ("prepared", 4, 4)
    finally:
        try:
            _reset_alembic_state()
            _upgrade_to("head")
        finally:
            engine.dispose()
