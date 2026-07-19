"""#1377: backfill workspace_connectors.locale to the worker Locale contract.

The bridge's WorkerConfigResponse.locale is ``Literal["en", "ja"]``; a
non-conforming stored value fails bridge-side validation of the WHOLE
config body and the tenant fails closed (``config_unavailable``). The
write boundary now normalizes/rejects at create+update time; this
migration repairs rows written before the fix:

* primary subtag ``en``/``ja`` (any case, BCP-47 suffix, ``_`` or ``-``
  separator) → the bare contract value
* anything else (including blank strings) → NULL, which the worker
  treats as "use the worker default locale" — fail-open, matching the
  vend-side normalization added with this change

``config_version`` is intentionally NOT bumped: the vended value for a
normalized row is identical to what the vend-side normalizer already
produces, so no worker refetch is needed.

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


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE workspace_connectors
            SET locale = CASE
                WHEN split_part(replace(lower(btrim(locale)), '_', '-'), '-', 1) = 'en'
                    THEN 'en'
                WHEN split_part(replace(lower(btrim(locale)), '_', '-'), '-', 1) = 'ja'
                    THEN 'ja'
                ELSE NULL
            END
            WHERE locale IS NOT NULL
              AND locale NOT IN ('en', 'ja')
            """
        )
    )


def downgrade() -> None:
    # Lossy normalization cannot be reversed; normalized values are valid
    # under the pre-#1377 schema too.
    pass
