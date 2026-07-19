"""#1365: erasure carve-out for secret_access_log's append-only trigger.

Replaces ``secret_access_log_no_mutate`` (e50) so an UPDATE touching
ONLY the identity columns ``(actor_user_id, recipient_identity)`` is
permitted — the #1278 ``memory_access_events`` carve-out pattern.
DELETE and TRUNCATE stay blocked; any UPDATE touching any other column
stays blocked. The account-erasure sweep is the intended (and only)
writer through this carve-out.

Tamper-evidence trade-off (GDPR Art.17 / APPI 第22条 precedence): rows
are HMAC hash-chained (``entry_hash = HMAC(key, prev_hash ||
canonical(entry))``). Pseudonymizing the identity columns makes the
stored ``entry_hash`` no longer recompute for EXACTLY the mutated rows;
chain LINKAGE (``prev_hash`` pointers) is untouched, so every other
row — and the chain topology itself — still verifies. Verifiers must
treat entry-hash mismatches on pseudonymized rows as expected (see
docs/ops/erasure-runbook.md §2.4).

Revision ID: e72_1365_secret_log_carveout
Revises: e71_1333_measurements
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e72_1365_secret_log_carveout"
down_revision = "e71_1333_measurements"
branch_labels = None
depends_on = None

# Columns the erasure sweep may UPDATE. Everything else stays immutable.
_CARVE_OUT = "'actor_user_id', 'recipient_identity'"


def upgrade() -> None:
    # The e50 triggers already reference this function by name —
    # CREATE OR REPLACE swaps the body without touching the triggers.
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION secret_access_log_no_mutate()
            RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'UPDATE' THEN
                    IF (to_jsonb(OLD) - ARRAY[{_CARVE_OUT}])
                       IS DISTINCT FROM
                       (to_jsonb(NEW) - ARRAY[{_CARVE_OUT}]) THEN
                        RAISE EXCEPTION
                            'secret_access_log is append-only; only '
                            '(actor_user_id, recipient_identity) may be '
                            'updated (erasure carve-out)'
                            USING ERRCODE = 'restrict_violation';
                    END IF;
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION
                    'secret_access_log is append-only; % is not permitted', TG_OP
                    USING ERRCODE = 'restrict_violation';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )


def downgrade() -> None:
    # Restore the e50 blanket version (no carve-out).
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION secret_access_log_no_mutate()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION
                    'secret_access_log is append-only; % is not permitted', TG_OP
                    USING ERRCODE = 'restrict_violation';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
