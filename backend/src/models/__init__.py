"""Data models for Kagura Memory Cloud."""

import importlib

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
import models.announcement_consent  # noqa: F401  # Issue #2761: announcement-email consent history
import models.beta_invite  # noqa: F401  # Issue #1581: closed-beta invite links
import models.deferred_point_delete  # noqa: F401  # Issue #2476: deferred forget point deletes
import models.erasure  # noqa: F401  # Vector cleanup jobs reference erasure_requests.
import models.file_objects  # noqa: F401  # Issue #485: file storage
import models.llm_call_log  # noqa: F401  # Issue #474: comprehensive call ledger
import models.llm_pricing  # noqa: F401
import models.management_grant  # noqa: F401  # Scoped integration delegation
import models.mcp_notice  # noqa: F401  # Issue #2762: service notices over MCP + consent
import models.measurement  # noqa: F401  # Issue #1333: HOW-MUCH measurement lane
import models.memory_access_event  # noqa: F401  # Issue #1278: agent access audit (P0-5)
import models.observation_credential  # noqa: F401  # Issue #2679: observability credentials
import models.operation_history  # noqa: F401  # Issue #2739: standalone create_all parity
import models.operations_status  # noqa: F401  # Issue #2768: scheduled job runs, LLM lanes
import models.public_mcp  # noqa: F401  # Issue #2675: anonymous public MCP endpoints
import models.public_mcp_usage  # noqa: F401  # Issue #2678: public MCP usage events
import models.referral  # noqa: F401  # Issue #1470: referral program ledger
import models.retrieval_feedback  # noqa: F401  # Issue #888: retrieval feedback signal
import models.secrets  # noqa: F401  # Issue #1128: zero-knowledge secret store
import models.sleep  # noqa: F401
import models.terms  # noqa: F401  # Issue #1665: terms-of-service acceptance history
import models.vector_erasure  # noqa: F401
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

# Register identity tables for standalone metadata consumers as well as the API.
importlib.import_module("models.oidc")
importlib.import_module("models.integration_entitlement")
importlib.import_module("models.sync_installation")

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
