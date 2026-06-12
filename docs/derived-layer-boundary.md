# Derived-Layer Boundary: raw is exportable, derived is the moat

- **Issue**: [#968](https://github.com/kagura-ai/memory-cloud/issues/968) (moat lever M2)
- **Status**: Adopted
- **Enforced by**: `backend/src/models/data_boundary.py` + `backend/tests/test_derived_layer_boundary.py`
- **Last updated**: 2026-06-11

## The rule

> **Raw memories are exportable; the derived/learned layer is the moat and is
> what compounds with use.**

Raw memories are commodity text. Users own them, and they are (rightly)
portable via the GDPR/JSON export work (#950). The defensible asset is the
**derived layer**: per-context Hebbian edge weights, embedding/importance
calibration, neural co-recall tuning, and Sleep-consolidated structure. That
layer cannot be reproduced by exporting raw text, and it gets better the
longer a context is used.

Every feature touching storage, export, Sleep, or edges must keep these two
properties simultaneously true:

1. **No leakage**: derived signal never moves onto the raw-export surface.
2. **Genuine accrual**: derived signal actually compounds with use — a
   "derived" artifact that is a pure function of the raw data at rest is not
   a moat, it is a cache.

## Layer definitions

| Layer | Definition | Portability |
|-------|------------|-------------|
| **Raw** | Content the user authored or ingested, plus its provenance. Recomputable by nobody — it *is* the user's data. | Fully exportable (#950). Erasable under GDPR (#360). |
| **Derived** | Structure the platform computed or learned **from usage**: co-activation, recall patterns, consolidation decisions, calibration. | Never exported. Erased with the account, but not portable. |
| **Operational** | Platform plumbing: auth, billing, quotas, audit, infra state. | Neither; governed by its own regimes (security, GDPR erasure, cost accounting). |

The machine-readable classification of **every** database table lives in
`backend/src/models/data_boundary.py`. A CI guard
(`backend/tests/test_derived_layer_boundary.py`) fails when a new table is
added without a classification, so the boundary decision is forced at
feature-development time, not discovered at export time.

## Artifact enumeration

### Raw (exportable)

| Table | What it holds |
|-------|---------------|
| `memories` | L1 `summary`, L2 `context_summary`, L3 `content`/`details`, `tags`, `type`, `scope`, user-set `importance`/`confidence`, provenance (`source`, `source_type`, `source_uri`), timestamps |
| `attachments` | User file metadata (filename, content type, size) |
| `file_objects` | R2 object records for user uploads (#485) |
| `agent_states` | User-set key/value run state (#889) |
| `contexts` | User-authored container name/description |
| `resources` | Connected source-of-truth resources |
| `resource_events` | Ingested raw source events |
| `resource_schemas` | User-registered resource schemas |

Also raw: **declared edges** — links the user explicitly created via
`create_edge` (rows in `neural_memory_edges` with `origin='declared'`). The
link topology and its user-set initial weight are user-authored data.

### Derived (the moat — never exported)

| Table | What it holds | Why it compounds |
|-------|---------------|------------------|
| `neural_memory_edges` | Hebbian (`origin='hebbian'`) and semantic (`origin='semantic'`) edge `weight`, `origin`, `confidence`, `edge_metadata` | Strengthened by co-recall, decayed nightly; encodes how *this* context is actually used |
| `graph_memory` | Legacy NetworkX graph JSON | Same learned graph, earlier storage format |
| `embedding_calibrations` | Top-k similarity percentiles (`p25`–`p99`), sample size, TTL | Recalibrated as the corpus grows; tunes seeding thresholds per workspace |
| `neural_config` | Neural tuning parameters | Operator/learned tuning of the learning loop itself |
| `sleep_reports` | Per-phase Sleep results (`edge_discovery_result`, `dedup_result`, `importance_result`, `consolidation_result`, `reindex_result`) | Consolidation history; each run builds on prior structure |
| `sleep_actions` | Individual consolidation decisions (merges, promotions, flags) | Same |
| `hub_tag_cache` | Computed tag co-occurrence hubs (`hub_tags`, `threshold_used`) | Recomputed nightly from observed tag usage |
| `memory_analyses` / `memory_analysis_assignments` / `memory_analysis_clusters` | Broadlistening cluster structure | Computed structure over the corpus |
| `retrieval_feedback` | Recall feedback signal (#888) | The only sanctioned learning input under the content-reuse policy |
| `bm25_idf_drift_log` | Corpus-derived index statistics | Tracks how the lexical index adapts to the corpus |

Ephemeral derived state also exists outside the DB: Redis
`co_activations:{user_id}` keys (in-flight Hebbian co-activation sums). Same
rule applies — serving-only, never exported.

### Why exporting derived fields leaks the moat

- `weight` + `origin` together let a client replay the Hebbian decay schedule
  and reconstruct the learned graph offline.
- Calibration percentiles (`p25`–`p99`) reverse-engineer the similarity
  thresholds that make seeding precise per-corpus.
- Sleep phase results expose consolidation decisions (what was merged,
  promoted, re-weighted) — the distilled output of the maintenance loop.

## Export surface vs serving surface

Derived signal is *supposed* to act on every query — that is its value. The
ban is on **bulk portability**, not on serving:

- **Export surface** (raw): `MemoryResponse`, `RecallResponse`,
  `ReferenceResponse`, `LinkedMemoryRef`, `PinnedMemoryItem`,
  `RelatedTagItem`, `RememberResponse`, `UpdateMemoryResponse` — and any
  future #950 export format. These must never grow a derived-only field.
  Enforced by `test_no_derived_fields_on_export_schemas`.
- **Serving surface** (derived-annotated): `explore()`'s
  `RelatedMemoryResponse` exposes per-query `activation` and `weight`;
  `recall()` exposes a transient `score`. These are query-scoped, ranked
  annotations — acceptable. A new endpoint that returns derived values
  *in bulk* (e.g. "list all edges with weights") is an export in disguise
  and crosses the boundary.

**Documented exception**: `LinkedMemoryRef.weight`. `reference()` surfaces
declared (user-authored) links only, and the user set the initial weight at
`create_edge`. Because edge rows are unique per ordered pair, the live value
can carry a Hebbian-strengthened component; this is accepted for declared
links. New surfaces must not copy this exception without the same
justification (registered in `EXPORT_SURFACE_FIELD_EXCEPTIONS`).

## Feature-review checklist

For any feature touching **storage, export, Sleep, or edges**, answer both
in the PR (this doc is the canonical checklist — reviewers, including
`/kagura-code-reviewer` and `/gh-issue-driven:ship` gate2, should recall and
apply it):

- [ ] **(a) No leakage** — the change does not move derived signal
  (edge weights/origins, calibration values, Sleep results, computed
  clusters/hubs) onto the raw-export surface. New tables are classified in
  `backend/src/models/data_boundary.py`; new export-shaped schemas are added
  to `EXPORT_SURFACE_SCHEMA_NAMES` so the leak guard covers them.
- [ ] **(b) Genuine accrual** — if the change adds derived state, that state
  actually compounds with use (strengthened/recalibrated/consolidated by
  usage over time), rather than being a static cache of the raw data.

## Why now

Pre-1.0 API freeze (#622) and export (#950) are both in flight. This boundary
is set deliberately before either solidifies, so export ships maximally
portable raw data without ever shipping the learned layer, and so new
derived features are designed to compound rather than to cache.
