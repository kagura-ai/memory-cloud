"""#1496: give back the retry budget that configuration failures wrongly spent.

``MAX_EMBEDDING_RETRIES`` is 3, and the sweep only claims a ``failed`` row
while ``embedding_retry_count < MAX``. A row that reaches 3 is therefore never
retried by anything again — correct for a poison row (bad input, oversized
text), wrong for a workspace that simply had no embedding credential
configured. That workspace burned all three attempts in about three minutes and
then stayed broken permanently: adding the key afterwards changed nothing.

The code fix stops the budget being spent that way, but it is NOT retroactive.
Rows already at the ceiling can never be claimed again, so without this
migration the fix ships pointing at a dead end and the counts the dashboard is
about to surface would name a problem with no path out of it. The production
deployment that prompted the issue had 467 such rows — every one at exactly 3 —
saved, counted, charged against quota, and absent from recall in both semantic
and keyword mode.

Resetting the counter (rather than trying to pattern-match ``embedding_error``,
which is a free-text provider string) puts them back through the FIXED code
path, which then classifies each one correctly on its own:

- a configuration failure is refunded again and keeps self-healing until the
  credential is fixed;
- a genuine poison row burns four fresh attempts and re-sticks, exactly as
  intended.

So the change is self-limiting: it does not decide which rows deserve another
chance, it lets the corrected logic decide. The one-time cost is ~4 sweep
attempts per affected row, bounded globally by the sweep's ``limit(20)`` per
30s tick.

Deliberately does NOT touch ``embedding_status``. These rows really did fail;
flipping them to ``pending`` would claim an outcome that has not happened yet
and would make them briefly indistinguishable from writes still in flight.
``embedding_retry_eligible_clause`` matches on ``failed`` plus budget plus
elapsed backoff, so the reset alone is enough to make them claimable.

Downgrade is a no-op: the pre-migration counter values are not preserved, and
restoring them would only re-strand the rows.

Revision ID: e77_1496_embed_retry_backfill
Revises: e76_1470_referral_program
"""

from alembic import op

# revision identifiers, used by Alembic.
# NOTE: alembic_version.version_num is varchar(32). The first draft of this
# revision id was 33 characters and CI's migration run failed with
# StringDataRightTruncationError — keep new ids comfortably under the limit.
revision: str = "e77_1496_embed_retry_backfill"
down_revision: str | None = "e76_1470_referral_program"
branch_labels: str | None = None
depends_on: str | None = None


# Matches config.constants.MAX_EMBEDDING_RETRIES at the time this migration was
# written. Deliberately inlined rather than imported: a migration records what
# it DID, and must not silently retarget if the constant is tuned later.
_MAX_EMBEDDING_RETRIES_AT_WRITE_TIME = 3


def upgrade() -> None:
    """Zero the counter on failed rows that had exhausted it."""
    op.execute(
        f"""
        UPDATE memories
        SET embedding_retry_count = 0
        WHERE embedding_status = 'failed'
          AND embedding_retry_count >= {_MAX_EMBEDDING_RETRIES_AT_WRITE_TIME}
          AND deleted_at IS NULL
        """
    )


def downgrade() -> None:
    """No-op — see the module docstring."""
