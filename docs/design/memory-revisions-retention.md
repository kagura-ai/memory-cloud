# `memory_revisions` retention

- **Status**: Design note only ([#2240](https://github.com/kagura-ai/memory-cloud/issues/2240)). Nothing prunes the revision log today, and this note does not add pruning.
- **Follows**: the [#2115](https://github.com/kagura-ai/memory-cloud/issues/2115) triage (retention is its own issue, opened when it is needed) and the per-key `details` provenance of [#2275](https://github.com/kagura-ai/memory-cloud/issues/2275).
- **Consumers**: whoever opens the retention issue; reviewers of any change that deletes `memory_revisions` rows.

## Today

`memory_revisions` holds one row per write event on a memory ([#2043](https://github.com/kagura-ai/memory-cloud/issues/2043), [#2044](https://github.com/kagura-ai/memory-cloud/issues/2044)). An `updated` row's `prior` holds the values that edit overwrote (only the changed fields). Rows leave only with their memory: a tombstone purge, a context delete or an account erasure deletes the memory, and the FK cascade deletes its log. `forget` keeps the log.

The log grows with the number of edits, and an edit that changes `content` copies the old `content` into `prior`. For now that growth is accepted. Retention is the lever if it becomes a cost, not diff chains: diff chains would change `reference(version)`, session rollback and erasure for a gain that retention gives more simply (#2115 triage).

## The constraint: whole logs only

**A retention job deletes a memory's whole log or none of it, never single revision rows.**

Several readers walk a memory's whole history, and each one gives a wrong answer if only part of it is gone:

- **Erasure provenance (#2275).** The erasure replays each candidate memory's `details` states from its first revision and gives each top-level key to the member who introduced its value. Drop an early row and a key the erased member introduced looks like it came from the next member who carried it. The erasure then keeps the erased member's value (a privacy failure), or reverts a surviving member's edit. The `erased_details_keys` digests and the erasure rows (`writer: erasure`) are part of that replay too.
- **Version rebuilds.** Version K is the live row with the `prior` of every `updated` row above K applied, newest first. A gap makes every version below it wrong, not just missing.
- **Session rollback and `changes_since`.** Rollback restores the `prior` of a session's first edit and records `undone_by` on the rows it handled. A missing row is an edit that can no longer be undone, and that nothing reports.

A memory with no log reads the same as a memory written before the log existed (before v0.113.0). Every reader already handles that case: `list_revisions` is empty, earlier versions cannot be rebuilt, rollback has nothing to undo, and erasure uses its no-log rules. A partial log has no such fallback.

## Shape of a retention job (when needed)

- **Unit**: one memory. Delete every row of `memory_revisions WHERE memory_id = :m` in one statement, or nothing.
- **Eligibility**: decided per memory, from the newest row of its log. For example: "the last write is older than N days", or "the memory has more than M revisions and its last write is older than N days". Never "rows older than N days", because that cuts logs in half.
- **Interaction with erasure**: run the job under the same per-memory locks the erasure takes (id-ordered, see `.claude/rules/backend.md`), so an erasure never replays a log that is being deleted. An erasure that is in progress or can still be re-run (`reprocess_erasures`) needs the logs of the memories it touches: skip a memory whose log has an erasure row newer than the retention window.
- **Visibility**: the API docs for `list_revisions`, `reference(version)` and `rollback_session` state the window, the same way they now describe memories from before v0.113.0.
- **Hot table**: deletes run in batches by memory id with `SKIP LOCKED`, like the other maintenance writes, and never in a migration.

## `idx_memory_revisions_actor`

Widening the f10 index from `(actor_user_id)` to `(actor_user_id, memory_id)` is **not done**. The erasure finds the subject's events by actor alone (the pseudonymize UPDATE and the session-provenance subquery), and the plain index serves both. The details-strip UPDATE correlates on `memory_id` through `idx_memory_revisions_memory`. No erasure measurement shows `memory_revisions` time as a cost yet. The trigger stays as #2115 set it: erasure time on `memory_revisions` becoming measurable in production. Widen the index (a new CONCURRENTLY revision) only then.
