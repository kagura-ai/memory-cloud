"""Merge the #887 (provenance + trust_tier) and #889 (agent_state) heads.

Both #887 (PR #907) and #889 (PR #905) were branched off ``e34`` and merged
to main independently, leaving two alembic heads. This is a no-op merge
revision that reconciles them into a single head so ``alembic upgrade head``
(and the deploy) works. The two migrations are independent — #887 alters
``memories.source_type`` + adds ``contexts.trust_tier``; #889 adds the
``agent_states`` table — so no reconciliation logic is needed.
"""

from collections.abc import Sequence

revision: str = "e36_merge_887_889"
down_revision: str | Sequence[str] | None = (
    "e35_887_provenance_trust_tier",
    "e35_889_agent_state",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
