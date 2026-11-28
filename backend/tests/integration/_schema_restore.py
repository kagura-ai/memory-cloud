"""Leave the test database as a migration round trip found it.

A round trip in the middle of a ``create_all`` file (``_reset_alembic_state``,
then upgrades) drops the schema and builds it again through every migration,
seed rows included (``neural_config``, ``llm_pricing``, ``oauth_clients``, ...).
Every later file in the same process would then read those rows, although it
assumes the ``create_all`` schema without them: the judge config, for one,
would come from the stored seed instead of the environment.

``restored_schema(engine)`` records the rows of every table, the sequences and
which kind of schema it found (built by the migrations when ``alembic_version``
exists, else by ``create_all`` as the ``async_engine`` fixture does). On exit it
builds that kind again (``upgrade head``, or ``create_all`` of the same tables)
and puts back exactly the recorded rows: rows a migration seeded before the
round trip stay, rows the round trip seeded go.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

_TABLES = text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
_SEQUENCES = text("SELECT sequencename, last_value FROM pg_sequences WHERE schemaname = 'public'")
_COLUMNS = text(
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name = :t AND is_generated = 'NEVER' "
    "ORDER BY ordinal_position"
)


@dataclass(frozen=True)
class _Snapshot:
    tables: frozenset[str]
    rows: dict[str, str]  # table -> json array of its rows (non-empty tables only)
    sequences: dict[str, int | None]  # sequence -> last_value (None: never used)

    @property
    def migrated(self) -> bool:
        return "alembic_version" in self.tables


def _quote(conn: Connection, name: str) -> str:
    return conn.dialect.identifier_preparer.quote_identifier(name)


def _snapshot(engine: Engine) -> _Snapshot:
    with engine.connect() as conn:
        tables = frozenset(conn.execute(_TABLES).scalars())
        rows: dict[str, str] = {}
        for table in tables:
            name = _quote(conn, table)
            data = conn.execute(
                text(f"SELECT coalesce(json_agg(t), '[]'::json)::text FROM {name} AS t")
            ).scalar_one()
            if json.loads(data):
                rows[table] = data
        sequences = dict(conn.execute(_SEQUENCES).tuples().all())
    return _Snapshot(tables=tables, rows=rows, sequences=sequences)


def _rebuild(engine: Engine, snapshot: _Snapshot) -> None:
    from models.auth import Base as AuthBase
    from models.memory import Base as MemoryBase
    from tests.integration.test_alembic_migrations import _reset_alembic_state, _upgrade_to

    if snapshot.migrated:
        _upgrade_to("head")
        return
    _reset_alembic_state()
    with engine.begin() as conn:
        for metadata in (AuthBase.metadata, MemoryBase.metadata):
            tables = [t for name, t in metadata.tables.items() if name in snapshot.tables]
            metadata.create_all(conn, tables=tables)


def _restore_rows(engine: Engine, snapshot: _Snapshot) -> None:
    with engine.begin() as conn:
        present = set(conn.execute(_TABLES).scalars())
        tables = sorted(snapshot.tables & present)
        # The rows go back as they were, in any order: no foreign key or
        # trigger fires for them (the test database's user owns it).
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        if tables:
            conn.execute(text(f"TRUNCATE {', '.join(_quote(conn, t) for t in tables)} CASCADE"))
        for table in tables:
            data = snapshot.rows.get(table)
            if data is None:
                continue
            name = _quote(conn, table)
            columns = ", ".join(
                _quote(conn, c) for c in conn.execute(_COLUMNS, {"t": table}).scalars()
            )
            conn.execute(
                text(
                    f"INSERT INTO {name} ({columns}) OVERRIDING SYSTEM VALUE "
                    f"SELECT {columns} FROM json_populate_recordset(NULL::{name}, CAST(:rows AS json))"
                ),
                {"rows": data},
            )
        existing = set(conn.execute(_SEQUENCES).scalars())
        for sequence, last_value in snapshot.sequences.items():
            if sequence not in existing:
                continue
            if last_value is None:
                conn.execute(text(f"ALTER SEQUENCE {_quote(conn, sequence)} RESTART"))
            else:
                conn.execute(
                    text("SELECT setval(CAST(:s AS regclass), :v, true)"),
                    {"s": _quote(conn, sequence), "v": last_value},
                )


@contextmanager
def restored_schema(engine: Engine) -> Iterator[None]:
    """Run the block, then rebuild the schema it found and put its rows back."""
    snapshot = _snapshot(engine)
    try:
        yield
    finally:
        _rebuild(engine, snapshot)
        _restore_rows(engine, snapshot)
