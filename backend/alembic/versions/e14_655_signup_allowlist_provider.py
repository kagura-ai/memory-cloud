"""Extend signup_allowlist to support Google OAuth (Issue #655).

Phase 1 of the admin-configurable signup gate (#358) assumed Google's OAuth
Consent Screen would gate signups via the test-users list. That assumption
holds only for sensitive scopes — for ``openid``/``email``/``profile`` (what
this app requests) Google in "Testing" status merely shows the "unverified
app" warning and lets users click through. The 2026-05-15 production audit
confirmed Google accounts outside the test-user list had completed OAuth.

This migration extends the GitHub-only ``signup_allowlist`` schema with a
provider dimension so the gate can match Google entries on the immutable
OIDC ``sub`` claim. GitHub rows backfill cleanly (``provider='github'``,
``subject_id=github_user_id``, ``subject_label=github_username``).

The ``github_user_id``/``github_username`` columns stay NOT NULL during the
migration window — physical drop is deferred to a future issue once all
admin tooling has switched to the provider-aware columns.

Backfilled data is NOT reversed on downgrade. The provider/subject_id/
subject_label values remain populated if rows are re-created via downgrade
+ re-upgrade (DDL reverses; data is a one-way migration, matching the
codebase convention from b03_396).

**Deployment assumption**: this migration assumes the project's single-server
deploy model (one ``kagura-api-green`` container per
``.claude/rules/dev-environment.md``) where the API is briefly stopped while
migrations apply. The transitional ``server_default`` on the three new
columns plus the belt-and-suspenders re-UPDATE immediately before the Step 5
NOT NULL tighten handle the case of a stray INSERT mid-migration (e.g. a
manual restore-from-backup that left a transaction open, or a future
zero-downtime deploy). In a true rolling deploy where new and old code
serve INSERTs concurrently for an extended window, do NOT rely on this
migration alone — coordinate by quiescing the admin allowlist API first.

Revision ID: e14_655_allowlist_provider
Revises: e13_474_pricing_seeds

NOTE: Revision IDs are capped at 32 chars because ``alembic_version.version_num``
is VARCHAR(32) in this database. The originally-drafted ID
``e14_655_signup_allowlist_provider`` was 33 chars and would have failed the
INSERT into ``alembic_version`` on ``alembic upgrade`` (asyncpg raises
``StringDataRightTruncationError``; some backends silently truncate, which
is worse — it leaves the DB in an unknown revision state). Shortened to
``e14_655_allowlist_provider`` (26 chars). Caught by Copilot review loop
on PR #657.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "e14_655_allowlist_provider"
down_revision = "e13_474_pricing_seeds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add provider/subject_id/subject_label columns to signup_allowlist.

    Phase 1 of #655. Existing rows are backfilled to ``provider='github'``,
    ``subject_id`` from ``github_user_id``, ``subject_label`` from
    ``github_username``. The new ``(provider, subject_id, source)`` UNIQUE
    + ``(provider, subject_id)`` INDEX replace the GitHub-only versions.
    """
    conn = op.get_bind()

    # Step 1: add all three columns NULLable with transitional server_defaults
    # so any concurrent INSERT during the migration window (e.g. an old-code
    # admin pod still serving the legacy GitHub allowlist API while a rolling
    # deploy is in flight) lands with non-NULL values for the new columns
    # and survives the Step 5 NOT NULL tightening.
    #
    # The transitional sentinels for subject_id/subject_label are dropped at
    # the end of Step 2 (DROP DEFAULT) so post-migration INSERTs from new
    # code must supply real values — the columns having a permanent default
    # is exactly what we are NOT trying to bake in.
    op.add_column(
        "signup_allowlist",
        sa.Column(
            "provider",
            sa.String(20),
            nullable=True,
            server_default=sa.text("'github'"),
        ),
    )
    op.add_column(
        "signup_allowlist",
        sa.Column(
            "subject_id",
            sa.String(255),
            nullable=True,
            # Empty-string sentinel. Step 2's backfill overwrites both
            # legacy NULL rows and any '' rows from the deploy window.
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "signup_allowlist",
        sa.Column(
            "subject_label",
            sa.String(255),
            nullable=True,
            server_default=sa.text("''"),
        ),
    )

    # Step 2: backfill from existing GitHub-only columns.
    #
    # Match on ``subject_id IS NULL OR subject_id = ''`` rather than the
    # previous ``provider IS NULL`` — under the new server_default scheme,
    # legacy rows have subject_id IS NULL while deploy-window inserts have
    # subject_id = '' (and ALSO provider='github' from the server_default
    # above). Both shapes need their subject_id / subject_label copied
    # from the legacy github_user_id / github_username so Step 5's
    # NOT NULL succeeds.
    #
    # signup_allowlist is admin-curated and small (single-digit to low-
    # thousand rows in any realistic deployment); a single UPDATE inside
    # the alembic transaction is the right shape — no chunking needed.
    conn.execute(
        sa.text(
            "UPDATE signup_allowlist "
            "SET provider = COALESCE(NULLIF(provider, ''), 'github'), "
            "    subject_id = github_user_id, "
            "    subject_label = github_username "
            "WHERE subject_id IS NULL OR subject_id = ''"
        )
    )

    # Step 2.5: drop the transitional server_defaults on subject_id /
    # subject_label. From here on, INSERTs MUST supply real values — the
    # empty-string sentinel was a deploy-window safety net only.
    op.alter_column("signup_allowlist", "subject_id", server_default=None)
    op.alter_column("signup_allowlist", "subject_label", server_default=None)

    # Step 3: drop old UNIQUE + INDEX before tightening to NOT NULL, so the
    # new constraints can sit on the new columns. ``type_="unique"`` on
    # drop_constraint matches the codebase convention from a99 / b03.
    op.drop_constraint(
        "uq_allowlist_user_source",
        "signup_allowlist",
        type_="unique",
    )
    op.drop_index(
        "ix_signup_allowlist_github_user_id",
        table_name="signup_allowlist",
    )

    # Step 4: add CHECK constraint on provider. For this small table we
    # don't need the NOT VALID + VALIDATE dance from b03 (which is for
    # large tables where the scan would block writes); a direct ADD
    # CONSTRAINT is fine.
    op.create_check_constraint(
        "valid_signup_allowlist_provider",
        "signup_allowlist",
        "provider IN ('github', 'google')",
    )

    # Step 4.5: belt-and-suspenders re-backfill. Step 2 caught the rows
    # that existed when the migration started; this catches any row that
    # may have slipped in via two paths:
    #
    # 1. Between Step 2 and Step 2.5: a stray INSERT during the deploy
    #    window picked up the server_default sentinel ('') from Step 1.
    # 2. **Between Step 2.5 and now**: after Step 2.5 drops the
    #    server_default, an old-code INSERT that doesn't know about the
    #    new columns lands them as NULL (column is still nullable until
    #    Step 5). Without the NULL branch here, such a row would fail
    #    Step 5's NOT NULL ALTER and abort the migration. (PR #657
    #    Copilot loop 2 finding #3 — caught a real correctness bug in
    #    the zero-downtime-deploy edge case.)
    #
    # Mirror Step 2's predicate exactly: ``subject_id IS NULL OR
    # subject_id = ''`` (and same for subject_label) covers both shapes.
    #
    # ``provider`` is intentionally NOT in this WHERE clause. Unlike
    # subject_id/subject_label whose server_default is dropped at Step
    # 2.5, the server_default on ``provider`` ('github') persists through
    # to the schema's permanent state — so any concurrent INSERT during
    # the entire migration window picks up provider='github' from the
    # default. The only path to provider IS NULL would be an explicit
    # ``INSERT ... (provider) VALUES (NULL)``, which requires the writer
    # to know about the new column — and any writer that does also
    # supplies subject_id/subject_label explicitly (see
    # ``add_to_allowlist_entry``). Old-code writers never name provider
    # so the default fires unconditionally.
    conn.execute(
        sa.text(
            "UPDATE signup_allowlist "
            "SET subject_id = github_user_id, "
            "    subject_label = github_username "
            "WHERE subject_id IS NULL OR subject_id = '' "
            "   OR subject_label IS NULL OR subject_label = ''"
        )
    )

    # Step 5: tighten the three new columns to NOT NULL. Safe now that
    # backfill (Step 2 + Step 4.5) has populated every existing row.
    op.alter_column("signup_allowlist", "provider", nullable=False)
    op.alter_column("signup_allowlist", "subject_id", nullable=False)
    op.alter_column("signup_allowlist", "subject_label", nullable=False)

    # Step 6: new UNIQUE + INDEX on the provider-aware columns. The UNIQUE
    # includes ``source`` for the same reason the old one did — a single
    # subject can legitimately have one ``manual`` row and one
    # ``github_sponsors`` row simultaneously.
    op.create_unique_constraint(
        "uq_allowlist_provider_subject_source",
        "signup_allowlist",
        ["provider", "subject_id", "source"],
    )
    op.create_index(
        "ix_signup_allowlist_provider_subject",
        "signup_allowlist",
        ["provider", "subject_id"],
    )


def downgrade() -> None:
    """Drop provider/subject_id/subject_label columns + restore old constraints.

    The backfilled data (provider='github', subject_id, subject_label) is
    NOT reversed — the columns themselves are dropped, so the values
    vanish with them. This matches the codebase convention from b03_396
    where DDL reverses but data backfill is treated as one-way.
    """
    # Reverse Step 6
    op.drop_index(
        "ix_signup_allowlist_provider_subject",
        table_name="signup_allowlist",
    )
    op.drop_constraint(
        "uq_allowlist_provider_subject_source",
        "signup_allowlist",
        type_="unique",
    )

    # Reverse Step 4 (CHECK on provider). ``IF EXISTS`` guards make the
    # downgrade idempotent in case an earlier rollback already dropped it.
    op.execute(
        sa.text(
            "ALTER TABLE signup_allowlist DROP CONSTRAINT IF EXISTS valid_signup_allowlist_provider"
        )
    )

    # Reverse Step 5 (NOT NULL → nullable) before dropping columns. Some
    # PG/SQLAlchemy combinations are picky about DROP COLUMN order with
    # constraints attached, so loosen first.
    op.alter_column("signup_allowlist", "subject_label", nullable=True)
    op.alter_column("signup_allowlist", "subject_id", nullable=True)
    op.alter_column("signup_allowlist", "provider", nullable=True)

    # Reverse Step 3 (old UNIQUE + INDEX restored)
    op.create_unique_constraint(
        "uq_allowlist_user_source",
        "signup_allowlist",
        ["github_user_id", "source"],
    )
    op.create_index(
        "ix_signup_allowlist_github_user_id",
        "signup_allowlist",
        ["github_user_id"],
    )

    # Reverse Step 1 (drop the three new columns; backfilled data NOT
    # reversed — columns and their values are dropped together)
    op.drop_column("signup_allowlist", "subject_label")
    op.drop_column("signup_allowlist", "subject_id")
    op.drop_column("signup_allowlist", "provider")
