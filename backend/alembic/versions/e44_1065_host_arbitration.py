"""Forge-resistant reinforce signal: feedback provenance + host-arbitration gate (#1065).

Issue #1065 (the "harden the signal" half of v0.36.0): an autonomous agent can
forge its own ranking boost by calling ``feedback(helpful=True)`` on whatever it
grounded on. This adds the substrate to distinguish that forgeable self-report
from a host-arbitrated, independent verdict, and to let recall ranking weight
only the unforgeable signal for untrusted callers.

- ``retrieval_feedback.provenance VARCHAR(16) NOT NULL DEFAULT 'agent'`` + CHECK
  IN ('agent','host'). Server-stamped; the public feedback path can only ever
  write 'agent'. The default backfills every existing row as 'agent' (correct —
  all historical feedback was agent/caller-emitted).
- ``context_search_configs.reinforce_require_host_arbitration BOOLEAN NOT NULL
  DEFAULT false`` — when true the #1048 re-rank counts only provenance='host'
  feedback. Default OFF → recall ranking is byte-identical to pre-#1065.

The provenance column lives on ``retrieval_feedback``, whose context/memory FKs
already cascade on delete, so the new aggregate is erasable for free (GDPR/APPI).

Revision ID: e44_1065_host_arbitration
Revises: e43_1027_share_keys

(Revision id kept <=32 chars for alembic_version.version_num.)
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "e44_1065_host_arbitration"
down_revision = "e43_1027_share_keys"
branch_labels = None
depends_on = None

# Byte-identical to the model CHECK literal (retrieval_feedback._ALL_FEEDBACK_PROVENANCES).
_PROVENANCE_CHECK = "provenance IN ('agent', 'host')"


def upgrade() -> None:
    op.add_column(
        "retrieval_feedback",
        sa.Column(
            "provenance",
            sa.String(16),
            nullable=False,
            server_default="agent",
        ),
    )
    op.create_check_constraint(
        "valid_retrieval_feedback_provenance", "retrieval_feedback", _PROVENANCE_CHECK
    )
    op.add_column(
        "context_search_configs",
        sa.Column(
            "reinforce_require_host_arbitration",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("context_search_configs", "reinforce_require_host_arbitration")
    op.drop_constraint("valid_retrieval_feedback_provenance", "retrieval_feedback", type_="check")
    op.drop_column("retrieval_feedback", "provenance")
