"""Data models for Kagura Memory Cloud."""

# Registered-table imports (required for SQLAlchemy FK resolution at app
# startup). ``models.auth.Workspace`` declares FKs to ``llm_pricing.id``
# (added by #494) and is queried by ``/admin/plans`` routes; without
# importing the target tables here, ``Base.metadata`` is incomplete and
# ``NoReferencedTableError`` fires on the first SELECT that triggers
# mapper resolution. Mirrors the explicit imports in
# ``backend/tests/conftest.py`` so production startup matches the
# test environment's import graph (#531).
import models.agent  # noqa: F401  # Issue #1274: Agent Registry (RFC-0002 P0-1)
import models.agent_state  # noqa: F401  # Issue #889: agent session-state lane
import models.analysis  # noqa: F401
import models.file_objects  # noqa: F401  # Issue #485: file storage
import models.llm_call_log  # noqa: F401  # Issue #474: comprehensive call ledger
import models.llm_pricing  # noqa: F401
import models.measurement  # noqa: F401  # Issue #1333: HOW-MUCH measurement lane
import models.memory_access_event  # noqa: F401  # Issue #1278: agent access audit (P0-5)
import models.retrieval_feedback  # noqa: F401  # Issue #888: retrieval feedback signal
import models.secrets  # noqa: F401  # Issue #1128: zero-knowledge secret store
import models.sleep  # noqa: F401
import models.worker_app  # noqa: F401  # Issue #1315: worker app identities
from models.config import ContextSearchConfig
from models.file_objects import FileObject, WorkspaceStorageUsage
from models.neural import NeuralConfig
from models.resource import (
    IndexerState,
    ResourceEvent,
    ResourceSchema,
    ResourceToken,
    WorkspaceAddon,
    WorkspaceConnector,
)
from models.worker_app import WorkerAppIdentity

__all__ = [
    "NeuralConfig",
    "ContextSearchConfig",
    "FileObject",
    "WorkspaceStorageUsage",
    "ResourceEvent",
    "ResourceSchema",
    "IndexerState",
    "ResourceToken",
    "WorkspaceAddon",
    "WorkspaceConnector",
    "WorkerAppIdentity",
]
