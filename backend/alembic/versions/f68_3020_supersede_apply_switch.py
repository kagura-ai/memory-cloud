"""#3020: the Sleep supersede-apply switch as a ``neural_config`` row.

Inserts ``sleep_supersede_apply_enabled`` = 'false' (category ``sleep``) so
the switch is listed and editable through the configuration API like its
neighbours. ``ON CONFLICT DO NOTHING``: a row an operator already created
keeps its value. No other row is touched.

Locks: a single-row INSERT into ``neural_config`` (ROW EXCLUSIVE on that
table only). No user table is locked.

Order: either order works. The previous release never reads the key; the new
code reads it through ``NeuralMemoryConfig.from_db`` and falls back to the
code default (off) while the row is missing. Re-runnable.

Downgrade deletes the row; the code default (off) applies again.

Revision ID: f68_3020_supersede_apply_switch
Revises: f66_3019_sleep_coverage
"""

from alembic import op

revision: str = "f68_3020_supersede_apply_switch"
down_revision: str | None = "f66_3019_sleep_coverage"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO neural_config (key, value, value_type, category, description, min_value, max_value) VALUES
            ('sleep_supersede_apply_enabled', 'false', 'bool', 'sleep', 'Let Sleep turn judge-verified supersede suggestions into supersedes links (#3020)', NULL, NULL)
        ON CONFLICT (key) DO NOTHING;
    """)


def downgrade() -> None:
    op.execute("DELETE FROM neural_config WHERE key = 'sleep_supersede_apply_enabled';")
