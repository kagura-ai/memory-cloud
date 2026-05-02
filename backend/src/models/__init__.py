"""Data models for Kagura Memory Cloud."""

# Registered-table imports (required for SQLAlchemy FK resolution at app
# startup). ``models.auth.Workspace`` declares FKs to ``llm_pricing.id``
# (added by #494) and is queried by ``/admin/plans`` routes; without
# importing the target tables here, ``Base.metadata`` is incomplete and
# ``NoReferencedTableError`` fires on the first SELECT that triggers
# mapper resolution. Mirrors the explicit imports in
# ``backend/tests/conftest.py`` so production startup matches the
# test environment's import graph (#531).
import models.analysis  # noqa: F401
import models.llm_pricing  # noqa: F401
import models.sleep  # noqa: F401
from models.config import ContextSearchConfig
from models.neural import NeuralConfig
from models.resource import (
    IndexerState,
    ResourceEvent,
    ResourceSchema,
    ResourceToken,
    WorkspaceAddon,
)

__all__ = [
    "NeuralConfig",
    "ContextSearchConfig",
    "ResourceEvent",
    "ResourceSchema",
    "IndexerState",
    "ResourceToken",
    "WorkspaceAddon",
]
