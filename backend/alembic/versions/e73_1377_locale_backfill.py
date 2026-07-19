"""#1377: backfill workspace_connectors.locale to the worker Locale contract.

The bridge's WorkerConfigResponse.locale is ``Literal["en", "ja"]``; a
non-conforming stored value fails bridge-side validation of the WHOLE
config body and the tenant fails closed (``config_unavailable``). The
write boundary now normalizes/rejects at create+update time; this
migration repairs rows written before the fix.

Normalization runs row-wise in Python (not SQL) so its whitespace
semantics are byte-identical to the runtime normalizer — PostgreSQL's
``btrim`` trims only ASCII spaces while Python's ``str.strip()`` also
trims tabs and U+3000 (full-width space, common in Japanese input); an
SQL implementation would silently downgrade a would-be ``ja`` row to
NULL. The mapping is fail-open like the vend boundary: primary subtag
``en``/``ja`` → the bare contract value, anything else → NULL (worker
default).

``config_version`` is bumped for every rewritten row: a bridge may have
cached the vend ETag for a body it then rejected, and without a version
bump the corrected locale would keep 304ing forever (review finding).
Rows already storing ``en``/``ja``/NULL are untouched.

Downgrade is a no-op: normalization is lossy (the original raw strings
are not preserved) and the normalized values remain valid for the old
code paths.

Revision ID: e73_1377_locale_backfill
Revises: e72_1365_secret_log_carveout
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e73_1377_locale_backfill"
down_revision = "e72_1365_secret_log_carveout"
branch_labels = None
depends_on = None


def _normalize(value: str) -> str | None:
    """Frozen copy of models.worker_runtime.normalize_worker_locale semantics
    at e73 time (migrations must not import drifting app code), except the
    non-conforming case degrades to None here instead of raising — the
    backfill is a read-boundary repair, not an admin write."""
    primary = value.strip().replace("_", "-").split("-", 1)[0].lower()
    if primary in ("en", "ja"):
        return primary
    return None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, locale FROM workspace_connectors "
            "WHERE locale IS NOT NULL AND locale NOT IN ('en', 'ja')"
        )
    ).fetchall()
    for row in rows:
        bind.execute(
            sa.text(
                "UPDATE workspace_connectors "
                "SET locale = :locale, config_version = config_version + 1 "
                "WHERE id = :id"
            ),
            {"locale": _normalize(row.locale), "id": row.id},
        )


def downgrade() -> None:
    # Lossy normalization cannot be reversed; normalized values are valid
    # under the pre-#1377 schema too.
    pass
