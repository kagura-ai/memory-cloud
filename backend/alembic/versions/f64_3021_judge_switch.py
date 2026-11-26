"""#3021: the consolidation judge switch as a ``neural_config`` row.

Inserts ``sleep_consolidation_judge_enabled`` = 'false' (category ``sleep``)
so the switch is listed and editable through the configuration API like its
neighbours. ``ON CONFLICT DO NOTHING``: a row an operator already created
keeps its value. No other row is touched; in particular the stored values of
``sleep_edge_discovery_enabled`` / ``sleep_importance_reeval_enabled`` stay as
they are.

Locks: a single-row INSERT into ``neural_config`` (ROW EXCLUSIVE on that
table only). No user table is locked.

Order: either order works. The previous release never reads the key; the new
code reads it through ``NeuralMemoryConfig.from_db`` and falls back to the
code default (off) while the row is missing. Re-runnable.

Downgrade deletes the row; the code default (off) applies again.

Revision ID: f64_3021_judge_switch
Revises: f63_3014_user_facts_lane
"""

from alembic import op

revision: str = "f64_3021_judge_switch"
down_revision: str | None = "f63_3014_user_facts_lane"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO neural_config (key, value, value_type, category, description, min_value, max_value) VALUES
            ('sleep_consolidation_judge_enabled', 'false', 'bool', 'sleep', 'Let the LLM judge decide promotion of the working memories consolidation rules leave borderline (#3021); rule promotion always runs', NULL, NULL)
        ON CONFLICT (key) DO NOTHING;
    """)


def downgrade() -> None:
    op.execute("DELETE FROM neural_config WHERE key = 'sleep_consolidation_judge_enabled';")
