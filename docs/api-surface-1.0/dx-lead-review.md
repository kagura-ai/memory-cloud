# DX Lead review — MCP tool surface (issue #622 AC4)

> Reviewed at `main` HEAD on 2026-06-20, after the #1054 first-time-agent
> usability work landed (all 45 MCP tools document return shapes; `reference()`
> returns the full memory; `reranker_provider` enum; `format:uuid` uniform).
>
> Scope: the **SDK-consumer / external-integrator** perspective — what becomes
> SEMVER-LOCKED at v1.0.0 (tool names, parameter names, required-vs-optional,
> enum values, same-name-different-role hazards). Agent-usability was covered in
> #1054 and is **not** re-litigated here.

The #1054 audit already addressed: response-shape documentation for all 45
tools, the `reference()` subset bug, `reranker_provider` enum, `format:uuid`
uniformity, and the multi-mode prose notes (`update_memory`, `forget`). This
review covers what remains for the **freeze**.

## Remaining lock-in risks, ranked

### 🔴 1. MCP tool OUTPUT shapes are not enumerated or frozen — biggest gap
The freeze covers input schemas (`mcp-input-schemas.md`) + REST response models
(`rest-response-models.md`), but the **45 MCP tool return shapes are equally
semver-locked at 1.0 and are not enumerated/frozen anywhere.** Evidence that
this is not theoretical: in the same session that wrote this review, `reference()`
was found to silently drop fields and `RecallConfidence` gained `prominence` —
exactly the drift the freeze exists to catch, on the un-enumerated output side.

- **Before freeze**: add `docs/api-surface-1.0/mcp-output-shapes.md` (the #1054
  work already produced every shape from each handler's success path — low cost
  to formalize).
- **Cost of NOT doing it**: output drift is invisible; a post-1.0 output change
  an SDK depends on is a silent breaking change with no guard.

### 🟠 2. Edge tools' `source_id`/`target_id` are MEMORY UUIDs, not named so
`create_edge` / `update_edge` / `delete_edge` take `source_id` / `target_id` =
**memory** UUIDs, while `merge_contexts` takes `source_context_id` /
`target_context_id` = **context** UUIDs. An SDK author reading `source_id`
reasonably assumes a generic id; the memory-vs-context distinction is
description-only.

- **Decide before freeze**: rename to `source_memory_id` / `target_memory_id`
  (clear, symmetric with `*_context_id`) OR accept and rely on docs.
- **Cost post-1.0**: rename = MAJOR bump across 3 tools + the SDK. **Pre-1.0
  rename is free — decide now.**

### 🟠 3. `details` is polymorphic across the surface
`remember` input `details` = object (Layer-3 JSONB); `recall_upcoming` output
`details` = string. Same field name, different type → SDK type generation
collides; consumers are surprised.

- **Decide before freeze**: rename the output (e.g. `detail_text`) or document
  the divergence as intentional. **Cost post-1.0**: type change/rename = MAJOR.

### 🟡 4. Convert remaining `⚠` items in `mcp-input-schemas.md` to explicit decisions
`type` free-string with the magic `'time'` value (enum-vs-open-vocabulary);
`describe_binding.context_id` as a binding *selector* (same name, different role);
the `oneOf`-not-expressible contracts (`update_memory` memory_id/external_id,
`forget` memory_id/query, `describe_binding` key_id/context_id). These carry `⚠`
today. **Do not leave any `⚠` unresolved at rc1** — for each, either freeze with a
rationale (`✅ DECIDED`) or fix.

### 🟡 5. The enumeration docs themselves have drifted (and were incomplete)
`rest-response-models.md` **omits every `schemas.py`-defined response model the
routes return** — `RememberResponse`, `RecallResponse`, `RecallConfidence`
(incl. the new `prominence` field), `ReferenceResponse`. The enumeration captured
only models defined *inside* the route files, missing the imported `schemas.py`
ones — i.e. the **core memory API responses are unenumerated.** A freeze run now
would lock an incomplete/stale snapshot.

- **Before freeze**: re-enumerate including `models/schemas.py` response models.
- The cheap input-schema drift (`recall` now requires `context_id`;
  `reranker_provider` now an enum) is fixed inline in this PR; the response-model
  re-enumeration is deferred to the rc1 freeze (see #6).

### 🔵 6. (Altitude) The "freeze" has no teeth — it should be an executable guard
The current freeze is **docs-only**; nothing fails CI when the surface drifts,
which is precisely why #1/#5 slipped in. The real freeze deliverable should be a
**snapshot test**: serialize the OpenAPI spec + the MCP input/output schemas to
committed fixtures and fail CI on any diff (review-gated updates). This both
*performs* the freeze and *keeps it honest*. Recommend upgrading AC2 from "freeze
docs against the rc1 tag" to "add the snapshot guard + freeze its fixtures at rc1".

## rc1 freeze checklist (carried on #622, due before v1.0.0-rc1)

- [ ] Add `mcp-output-shapes.md` (output enumeration) — finding #1
- [ ] Decide & apply: `source_id`/`target_id` → `source_memory_id`/`target_memory_id`? — finding #2
- [ ] Decide & apply: `recall_upcoming.details` polymorphism — finding #3
- [ ] Convert all remaining `⚠` to `✅ DECIDED` or fix — finding #4
- [ ] Re-enumerate `rest-response-models.md` incl. `schemas.py` response models — finding #5
- [ ] Add the OpenAPI + MCP-schema snapshot test; freeze fixtures against the rc1 tag — finding #6
- [ ] Link each enumeration file to the v1.0.0-rc1 commit (original AC2)

Findings #2 and #3 are the only ones that want a **pre-rc1 code decision** (renames
are free now, MAJOR later); the rest are enumeration/tooling that belong in the
rc1 freeze pass.
