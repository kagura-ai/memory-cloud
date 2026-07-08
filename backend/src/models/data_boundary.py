"""Raw-vs-derived data boundary registry (Issue #968, moat lever M2).

The rule: **raw memories are exportable; the derived/learned layer is the
moat and is what compounds with use.** Raw artifacts (user-authored text,
tags, provenance) must be fully portable — GDPR/JSON export (#950) covers
them. Derived artifacts (Hebbian edge weights, embedding calibration,
Sleep-consolidated structure) are platform-learned and must never move onto
the raw-export surface.

This module is the machine-readable half of that rule; the prose half lives
in ``docs/derived-layer-boundary.md``. ``tests/test_derived_layer_boundary.py``
enforces both: every ORM table must appear in exactly one of the three sets
below, and the export-surface schemas must never grow a derived-only field.

When you add a table, classify it here deliberately:

- ``RAW_EXPORTABLE_TABLES`` — user-authored or user-ingested content and its
  provenance. The user can take this with them; losing it loses nothing the
  platform learned.
- ``DERIVED_MOAT_TABLES`` — structure the platform computed or learned from
  usage. Cannot be reconstructed from a raw export; accrues value with use.
- ``OPERATIONAL_TABLES`` — platform plumbing (auth, billing, quotas, audit,
  infra state). Neither raw content nor learned structure; governed by its
  own regimes (auth/security, GDPR erasure, cost accounting).
"""

from __future__ import annotations

RAW_EXPORTABLE_TABLES: frozenset[str] = frozenset(
    {
        "memories",  # L1/L2/L3 text, tags, importance, source provenance
        "attachments",  # user file metadata (blobs in R2)
        "file_objects",  # R2 object records for user uploads (#485)
        "agent_states",  # user-set key/value run state (#889)
        "contexts",  # user-authored container name/description
        "resources",  # connected source-of-truth resources
        "resource_events",  # ingested raw source events
        "resource_schemas",  # user-registered resource schemas
    }
)

DERIVED_MOAT_TABLES: frozenset[str] = frozenset(
    {
        "neural_memory_edges",  # Hebbian/semantic edge weights + origin
        "graph_memory",  # legacy NetworkX graph JSON storage
        "embedding_calibrations",  # similarity percentile calibration (p25–p99)
        "neural_config",  # neural tuning parameters
        "sleep_reports",  # Sleep consolidation phase results
        "sleep_actions",  # individual consolidation decisions
        "hub_tag_cache",  # computed tag co-occurrence hubs
        "memory_analyses",  # Memory Analysis runs
        "memory_analysis_assignments",  # memory→cluster assignments
        "memory_analysis_clusters",  # computed cluster structure
        "retrieval_feedback",  # learning signal (content-reuse policy: feedback only)
        "bm25_idf_drift_log",  # corpus-derived index statistics
    }
)

OPERATIONAL_TABLES: frozenset[str] = frozenset(
    {
        "users",
        "user_oauth_providers",
        "oauth_authorization_codes",
        "oauth_clients",
        "oauth_device_codes",
        "oauth_tokens",
        "api_keys",
        "share_keys",  # context-scoped read-only TTL share credential (#1027)
        "external_api_keys",
        "workspaces",
        "workspace_members",
        "workspace_invitations",
        "workspace_addons",
        "workspace_connectors",
        "workspace_storage_usage",
        # break-glass dual-control approval workflow (#1113) — governance plumbing,
        # like erasure_requests/audit_logs; no user content, no learned structure
        "workspace_ownership_force_transfer_requests",
        "context_members",
        "context_search_configs",
        "plan_changes",
        "usage_stats",
        "audit_logs",
        "erasure_requests",
        "signup_allowlist",
        "signup_gate_config",
        "config_overrides",
        "llm_call_log",
        "llm_pricing",
        "mcp_tool_descriptions",
        "indexer_state",
        "resource_tokens",
        "sleep_report_llm_usage",  # cost telemetry, not consolidation output
        # Zero-knowledge secret store (#1128) — security plumbing, governed by
        # the auth/security regime + GDPR erasure (cascade on workspace delete).
        # The server holds only opaque age ciphertext + public keys (never
        # plaintext); these are operational credentials, not raw user knowledge
        # (not on the memory export surface) and not learned/derived structure.
        "recipient_pubkeys",
        "secrets",
        "secret_versions",
        "secret_grants",
        "secret_access_log",  # append-only, tamper-evident audit (own regime)
    }
)

# Field names that exist only on derived-layer artifacts. They must never
# appear on an export-surface schema — exporting them would leak the learned
# structure (edge weights/decay behavior, calibration thresholds, Sleep
# decisions) into the portable layer.
DERIVED_ONLY_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "weight",  # edge co-activation strength (see exception below)
        "activation",  # spreading-activation strength (serving-only)
        "origin",  # hebbian/semantic/declared discriminator → leaks decay rules
        "edge_metadata",
        "p25",
        "p50",
        "p75",
        "p90",
        "p95",
        "p99",
        "edge_discovery_result",
        "dedup_result",
        "importance_result",
        "consolidation_result",
        "reindex_result",
        "hub_tags",
        "threshold_used",
    }
)

# Pydantic schemas in models/schemas.py that form the raw-export surface:
# the user-facing memory read/write shapes a portability export (#950) would
# be built from. The serving surfaces (explore()'s RelatedMemoryResponse,
# ExploreResponse) are deliberately NOT listed — derived signal is allowed
# to annotate per-query serving responses; it is bulk export that is banned.
# See docs/derived-layer-boundary.md.
EXPORT_SURFACE_SCHEMA_NAMES: frozenset[str] = frozenset(
    {
        "MemoryResponse",
        "RecallResponse",
        "ReferenceResponse",
        "LinkedMemoryRef",
        "PinnedMemoryItem",
        "RelatedTagItem",
        "RememberResponse",
        "UpdateMemoryResponse",
    }
)

# Documented exceptions to the derived-field ban, schema name → field names.
# LinkedMemoryRef.weight: reference() surfaces declared (user-authored) links
# only, and the user set the initial weight at create_edge. The live value
# can carry a Hebbian-strengthened component (the edge row is shared per
# ordered pair), which is accepted for declared links — but new export
# surfaces must not copy this exception without the same justification.
EXPORT_SURFACE_FIELD_EXCEPTIONS: dict[str, frozenset[str]] = {
    "LinkedMemoryRef": frozenset({"weight"}),
}
