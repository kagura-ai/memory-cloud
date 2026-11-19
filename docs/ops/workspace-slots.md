# Workspace slot suspension (#2918)

An owner may own as many workspaces as their **allowance**: `1 + slot bonus +
the grant of the highest tier they own` (#1550: Free 0, Basic 0, Pro 2, Max 19,
so 1 / 1 / 3 / 20 with no bonus), or the number of their workspaces under a
billing contract when that is higher, because a workspace somebody pays for is
never suspended. Only owned workspaces count; membership in someone else's
workspace takes no slot.

When an event **lowers** that allowance below the number of workspaces the owner
has active (a subscription ends, a system admin lowers a plan or the slot
bonus), the excess owned workspaces are **suspended**: their data is kept,
nothing is deleted, and every member and every credential is refused with
`SLOT-001` (MCP: `workspace_suspended`) until the workspace is resumed. The
owner keeps a management path: they can see the status on the account page
(`/profile/workspaces`), export contexts, delete the workspace, open billing
for it, revoke credentials, and choose which workspaces stay active. When slots
return (an upgrade, a bonus, a deleted or transferred workspace), suspended
workspaces are resumed within the new allowance, each with its own plan.
Suspension never changes a plan, an add-on, the capacity lock (#1941) or any
data, and never starts a capacity purge; the purge pauses for a suspended
workspace and starts a fresh 30 days after it resumes.

Without a saved selection the default order applies at once and the owner is
flagged `selection_required` (shown on the account page at the next sign-in):
the owner's current workspace first, then the higher plan, then the older
workspace. A saved selection (the account page, `PUT
/api/v1/me/workspace-slots/selection`) is applied first; while something is
suspended a save applies at once (a swap). Saving the same list again changes
nothing (the same `version`). An empty saved list means the default order: saving
it clears the prompt, but it is not a choice, so a later drop that suspends
something prompts again (as does a saved list whose workspaces are all gone).

A suspended workspace that gets a paid plan comes back. When that plan grants no
extra slot (Basic), the lowest-priority uncontracted active workspace is
suspended in its place (a swap); when it does (Pro, Max), the others resume too.

Events that are not a drop only resume. An owner who is already over the
allowance (created before `ENFORCE_WORKSPACE_CAP`, or after a plan change while
the feature was off) keeps every workspace until their next drop.

## The setting

`WORKSPACE_SLOT_SUSPENSION`, read at startup:

| Value | Behaviour |
|---|---|
| `off` (default) | Nothing changes. No statement touches the slot tables; the sweep is not scheduled; the account API answers 404. |
| `shadow` | Triggers compute what they would do and log `workspace_slots_would_change`; they never suspend. They do resume: rows that should resume are deleted (a row lock that is not free at once makes them give up and log `workspace_slots_shadow_failed`; the caller is never delayed or failed). The gates ignore every row, so nobody is refused. |
| `enforce` | Drops suspend, everything else resumes; the gates refuse suspended workspaces; the account API and page are available. Requires `ENFORCE_WORKSPACE_CAP=true` (startup refuses otherwise). |

`shadow` and `enforce` refuse to start until migration
`f56_2918_workspace_slots` has created the two tables (`alembic upgrade head`),
and when the `PLAN_*_OWNED_WORKSPACE_GRANT` overrides decrease along the plan
order (Free, Basic, Pro, Max): a plan change whose tier grant does not go down
is treated as one that cannot lower the allowance, which holds only for grants
that never decrease.
The migration takes no lock on an existing table, so it can run before or after
the new code.

## Rollout

1. Deploy with `off` (the default) and run `alembic upgrade head`.
2. The payment service must retry `409 RES-002` with
   `details.reason: workspace_slots_busy` (below) before step 4.
3. Switch to `shadow` for one billing cycle. Read the
   `workspace_slots_would_change` events: every owner listed would lose access
   to the `would_suspend` workspaces at that event. Owners over the allowance
   from before lose all their excess at their first drop of any size; this is
   where you see them.
4. Switch to `enforce` once every serving process runs this release.

## Triggers

| Trigger | Suspends | Owner lock | When the lock is busy | On an unexpected error |
|---|---|---|---|---|
| Ordered billing snapshot (`PUT /internal/workspaces/{id}/billing-snapshot` / `integration-entitlements`, a plan applied) | yes | tried with a short backoff (about 0.75 s) while the workspace row is held, before the sync stop | a change that may lower the allowance: `409 RES-002`, `workspace_slots_busy`, nothing committed; a change that cannot (an upgrade, the same plan): commits, logs `workspace_slots_deferred`, the sweep finishes the resume | may lower: the delivery fails; cannot lower: committed and deferred |
| Legacy plan push (`PUT /internal/workspaces/{id}/plan`) | yes | as above | as above | as above |
| System-admin plan change (`PUT /api/v1/admin/plans/workspaces/{id}/plan`) | yes | as above | as above (`409` to the admin) | as above |
| Slot-bonus change (`PATCH /api/v1/admin/users/{id}/workspace_slot_bonus`) | yes | waited for (5 s) first | `503` as before | the request fails |
| Owner's selection (account page) | swap only | waited for (5 s) first, then the owner's account row (a save during the account's erasure waits for it and answers 404) | `409 RES-002`, `workspace_slots_busy` | the request fails |
| Workspace delete, voluntary transfer, force transfer and its dual-control approval | no (resume only) | after the commit, tried with the short backoff (it is on the response path; for a delete, after the storage and vector cleanup) | logged `workspace_slots_deferred`, left to the sweep | logged, left to the sweep (a database pool timeout too); the delete or transfer is never failed by it |
| Sweep (every 15 minutes, owners with suspension rows) | no | one try per owner | that owner is skipped this round | logged, the next owner continues |

Converge **only by the sweep**: account erasure's automatic ownership hand-over,
admin user deletion (`DELETE /api/v1/admin/users/{id}` and
`python -m src.cli.delete_admin`), and a RAISED `PLAN_*_OWNED_WORKSPACE_GRANT`
(it moves every allowance with no event; the sweep resumes within it). A
LOWERED grant suspends nothing: owners above the new allowance keep their
workspaces until their next drop.

Each sweep run visits every owner with suspension rows once, in batches, for up
to about a minute; with more owners than a minute's worth, the next run
continues where it stopped. A resume left to the sweep therefore happens within
one sweep interval (15 minutes) unless a run cannot visit every owner in its
minute.

A **voluntary transfer** in `enforce` needs a free slot at the recipient: the
recipient's allowance after the transfer (counting the incoming workspace's
tier and contract, so an incoming Pro or Max can make room) must be above the
number of workspaces they have active now. Their saved selection plays no part,
and a workspace under a billing contract into a full account is refused too.
Otherwise `409 SLOT-004` ("the new owner has no free workspace slot"; nothing
about the recipient's workspaces is disclosed). A busy recipient lock answers
the retryable `409 RES-002`. Force transfers, their approval, erasure and admin
deletion transfers are exempt.

Account erasure and user deletion delete the subject's rows in both tables in
every mode (also before the migration: the delete is skipped when the tables do
not exist).

## Log events

| Event | Meaning |
|---|---|
| `workspace_slots_reconciled` | `enforce`: an owner's suspensions changed (`suspended`, `resumed`, `pruned`, `drop`, `cap`, `allowance`, `cause`). |
| `workspace_slots_would_change` | `shadow`: what `enforce` would have done (`would_suspend`, `resumed`). |
| `workspace_slots_deferred` | A resume could not run now (`reason: lock_busy` or `error`); the sweep finishes it. Repeated `error` for one owner needs a look. |
| `workspace_slots_busy` | A change that may lower an allowance found the owner lock busy and was refused with the retryable 409. |
| `workspace_slots_shadow_failed` | `shadow` reconcile raised; the caller was not affected. Fix before `enforce`: in `enforce` the same error fails allowance-lowering deliveries. |
| `workspace_transfer_refused_no_slot` | A voluntary transfer was refused with `SLOT-004`. |
| `workspace_slot_sweep_done` | Sweep counts (`owners`, `resumed`, `pruned`, `busy`, `failed`). |

Alert on `workspace_slots_deferred` with `reason: error` repeating for one owner,
and on `workspace_slots_busy` repeating: one owner's deterministic reconcile
error blocks every allowance-lowering delivery for that owner.

## What the payment service must retry

`409`, `RES-002`, `details.reason: workspace_slots_busy` on the snapshot and
legacy plan routes is retryable with the same body, like `sync_transition_busy`
([Ordered workspace billing](ordered-workspace-billing.md)): nothing was
committed. It happens when the owner's slot lock is held by another of their
transactions (another delivery for a sibling workspace, their own workspace
creation, which can hold it for seconds, or the sweep). Retry with backoff.

## Escape hatches

- **Restore all access at once:** `WORKSPACE_SLOT_SUSPENSION=shadow` and
  restart. The gates ignore the rows; nothing new is suspended. The rows are
  not kept current: drops during `shadow` are never applied later, and a
  workspace that would have resumed meanwhile keeps its row until a reconcile
  of its owner runs.
- **Resume one owner:** raise their slot bonus
  (`PATCH /api/v1/admin/users/{id}/workspace_slot_bonus`); the change resumes
  within the new allowance at once.
- **CLI** (inside the API container):

  ```
  python -m src.cli.workspace_slots status [--owner <user_id>]
  python -m src.cli.workspace_slots reconcile [--owner <user_id>] [--even-when-off]
  ```

  `reconcile` resumes and prunes only, never suspends. While the setting is
  `off` it refuses unless `--even-when-off` is given. An owner whose reconcile
  fails or whose lock stays busy is reported and the run goes on; the exit code
  is then 1.

## Mode switches

- Drops that happen while the mode is `off` or `shadow` are **never applied
  later** (a replayed delivery does not run the triggers again).
- `enforce -> shadow` restores all access at once and stops new suspensions.
- `enforce -> off`: no code touches the rows. Before switching back on, run
  `python -m src.cli.workspace_slots reconcile --even-when-off` so rows that no
  longer hold (a workspace upgraded or deleted meanwhile) are gone first; a row
  on a workspace under a billing contract is never honoured either way.
- Downgrading the migration drops both tables and every suspension: only with
  the mode `off`.

## Accepted risks

- An owner's own workspace creation can hold the owner lock for seconds;
  allowance-lowering deliveries for that owner get the retryable 409 meanwhile.
- A cancellation landing just before a sibling workspace's upgrade can suspend
  workspaces for a moment; the upgrade resumes them.
- A swap planned on a resume-only path (the sweep, a post-commit reconcile, a
  deferred upgrade) only resumes: the owner ends one over the allowance until
  their next drop.
- Upgrading a suspended workspace to a tier with no slot grant (Basic) swaps out
  the lowest-priority uncontracted workspace.
- A Free workspace with a paid integration right can be suspended by the default
  order; the owner keeps it by selecting it.
- Members lose access without an email; MCP-only owners learn it from the
  refusal.
- Transferring or deleting the top-tier workspace suspends nothing.
- A suspended workspace keeps over-capacity data while it stays suspended (the
  purge pauses); swapping which workspace is suspended restarts its purge clock.
- Presigned file URLs issued before the suspension stay valid until they expire.
