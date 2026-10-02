"""#1008: opaque public ids for keys, tokens and invitations.

Adds ``public_id`` to ``api_keys``, ``share_keys``, ``resource_tokens`` and
``workspace_invitations``: ``<prefix>_`` + 22 base62 characters (``akey``,
``skey``, ``rtok``, ``winv``). The API, the MCP tools and new audit rows use
it; the integer ``id`` stays the primary key for joins and foreign keys.

Steps per table:

1. add ``public_id`` as a nullable ``varchar(32)``;
2. backfill every existing row — revoked, expired and accepted rows
   included — with a base62 id from ``secrets``, in batches of 1000 (the
   generator is inlined: migrations do not import application code);
3. set the DB-side default ``'<prefix>_' || 22 hex chars of
   gen_random_uuid()`` (hex is a subset of base62, so it matches the same
   pattern), so an app instance from before this release that inserts a row
   without ``public_id`` during a rolling deploy still succeeds;
4. ``SET NOT NULL`` and add the ``<table>_public_id_key`` unique constraint.

The default is set after the backfill rather than with the column so that
existing rows get base62 ids instead of the hex fallback; the whole upgrade
runs in one transaction and ``ALTER TABLE`` holds the table lock until
commit, so no concurrent insert can land between steps 1 and 3.

Old audit rows keep the integer id they were written with
(``api_key:<int>``). Map one to its public id with
``SELECT public_id FROM api_keys WHERE id = <int>``.

Downgrade drops the columns: the public ids are gone for good, and a later
upgrade issues different ones. Clients holding a public id would have to
list the resource again.

Revision ID: e91_1008_public_ids
Revises: e90_1784_identity_links

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import secrets
import string

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e91_1008_public_ids"
down_revision: str | None = "e90_1784_identity_links"
branch_labels: str | None = None
depends_on: str | None = None

_TABLES: dict[str, str] = {
    "api_keys": "akey",
    "share_keys": "skey",
    "resource_tokens": "rtok",
    "workspace_invitations": "winv",
}
_BASE62 = string.digits + string.ascii_uppercase + string.ascii_lowercase
_BODY_LENGTH = 22
_BATCH = 1000


def _new_public_id(prefix: str) -> str:
    return f"{prefix}_" + "".join(secrets.choice(_BASE62) for _ in range(_BODY_LENGTH))


def _server_default(prefix: str) -> str:
    # Must match utils.public_id.public_id_server_default verbatim, or the
    # create_all-vs-alembic drift test fails.
    return f"'{prefix}_' || substr(replace((gen_random_uuid())::text, '-', ''), 1, {_BODY_LENGTH})"


def _backfill(table: str, prefix: str) -> None:
    bind = op.get_bind()
    select_batch = sa.text(
        f"SELECT id FROM {table} WHERE public_id IS NULL ORDER BY id LIMIT :n"  # noqa: S608
    )
    update_row = sa.text(f"UPDATE {table} SET public_id = :pid WHERE id = :id")  # noqa: S608
    while True:
        ids = bind.execute(select_batch, {"n": _BATCH}).scalars().all()
        if not ids:
            return
        bind.execute(update_row, [{"id": i, "pid": _new_public_id(prefix)} for i in ids])


def upgrade() -> None:
    for table, prefix in _TABLES.items():
        op.add_column(table, sa.Column("public_id", sa.String(32), nullable=True))
        _backfill(table, prefix)
        op.alter_column(
            table,
            "public_id",
            existing_type=sa.String(32),
            server_default=sa.text(_server_default(prefix)),
            nullable=False,
        )
        op.create_unique_constraint(f"{table}_public_id_key", table, ["public_id"])


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_constraint(f"{table}_public_id_key", table, type_="unique")
        op.drop_column(table, "public_id")
