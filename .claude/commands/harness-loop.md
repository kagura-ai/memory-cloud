---
description: Run the full Planner → Evaluator → Generator harness loop for a single GitHub issue, end-to-end
arguments:
  - name: issue
    description: GitHub issue number to run the harness against
    required: true
  - name: resume
    description: "Optional run_id (hr-YYYYMMDD-NNN) to resume from cached state instead of starting fresh"
    required: false
  - name: dry-run
    description: "If set, run only Phase A (Plan) and stop after writing the contract file — no Generator/Evaluator/PR"
    required: false
---

# /harness-loop

Top-level orchestrator for the memory-cloud development harness. Runs one
full Planner → gate1 → (Generator → Evaluator)\* → Quality → gate2 → PR →
gate3? → Persist sequence from a single GitHub issue.

**Authoritative schema**: `.claude/rules/harness-contract.md`. Read it first,
every time. The sections that govern this command are **Top-level contract
schema** (gates + budget), **Channel enum**, **Run record schema**, and
**Forbidden patterns**.

You are the main agent running this command. You will spawn three
specialized subagents (`harness-planner`, `harness-evaluator`,
`harness-generator`) via the `Task` tool, dispatch CxO/PhD reviewers at
gate boundaries, run `/quality` and `/self-review` between the inner loop
and gate2, and finally create the PR.

## Inputs

Parsed from `$ARGUMENTS`:

- `<issue>` (required) — GitHub issue number
- `--resume <run_id>` (optional) — restore from
  `~/.cache/memory-cloud-harness/runs/<run_id>/` instead of running Phase A
- `--dry-run` (optional) — stop after writing the contract; do not invoke
  Generator/Evaluator and do not open a PR

## Pre-flight checks

Before doing anything, run all four checks. Any failure aborts with a
single-block reason and an `outcome: aborted` run record.

1. **5-hour rate limit** — read `~/.claude/statusline-command.sh` output
   (or whatever the harness's standard usage probe returns) and abort if
   `five_hour.used_percentage > 80`. The threshold is the rule file's
   `abort_if_five_hour_pct_above` default.
2. **Issue exists and is open** — `gh issue view <issue> --json state` →
   abort if state ≠ `OPEN`.
3. **Branch hygiene** — current branch is either `main` or already a
   feature branch matching `<issue>-*`. If on an unrelated branch, abort.
4. **Cache dir writable** — `mkdir -p ~/.cache/memory-cloud-harness/runs/`
   and verify write access.

If `--resume <run_id>` is given, also verify
`~/.cache/memory-cloud-harness/runs/<run_id>/contract.json` exists and
parses as JSON. If it does not, abort with an explanatory error rather
than silently starting fresh.

## Phase A — Plan

If `--resume` was given, **skip this phase** and load the existing
`contract.json` from the cache directory.

Otherwise, spawn the Planner subagent:

```
Task tool:
  subagent_type: harness-planner
  description: "Plan harness contract for #<issue>"
  prompt: "Plan a harness contract for GitHub issue #<issue>.
           Read .claude/rules/harness-contract.md first.
           Write the contract to ~/.cache/memory-cloud-harness/runs/<run_id>/contract.json
           and return the run_id and contract summary."
```

After the Planner returns:

- Read the contract file from the cache directory
- Validate it parses as JSON
- Extract `run_id`, `contracts[]`, `gates`, `budget` for use by later phases

If `--dry-run` was set, print the contract summary and stop here.

## Phase B — gate1 (Planner review)

Read `contract.gates.gate1_planner_review`. If `enabled: false`, skip this
phase entirely and proceed to Phase C.

Otherwise, dispatch each role in `reviewers[]` via the `Task` tool with
`subagent_type` matching the slug exactly (e.g., `claude-c-suite:cto`,
`claude-phd-panel:db`). The reviewer reads the contract file and returns a
verdict in `[C]/[W]/[I]` format.

**Loop-back rule**: If any reviewer returns at least one `[C]` finding,
return to Phase A with a note pointing the Planner at the contract entries
that were challenged. The Planner emits a revised contract under a fresh
`run_id`. The original cache directory is preserved for diff inspection.

`[W]` and `[I]` findings are recorded in the run record but do not block
gate1. Maximum two reviewers per gate per the rule file's gate cap.

## Phase C — Inner loop (Generator → Evaluator)

Initialize:

```
iteration = 1
max_iterations = contract.budget.max_iterations
```

Each pass of the loop:

### C.1 — Generator

> **Note (Phase 4 status)**: The `harness-generator` subagent is authored
> separately as Phase 4 of the harness loop and lands via its own PR. Until
> that PR merges, this command's Generator step is a forward reference. To
> dry-run this command before Phase 4 lands, pass `--dry-run` and stop
> after Phase A.

```
Task tool:
  subagent_type: harness-generator
  description: "Generate iteration <iteration> for run <run_id>"
  prompt: "Read the contract at
           ~/.cache/memory-cloud-harness/runs/<run_id>/contract.json and the
           latest verdict at verdict-<iteration-1>.json (if iteration > 1).
           Implement the changes needed to make every contract pass.
           Do not modify .claude/, .github/, or other meta files.
           Return a one-line summary of files touched and lines changed."
```

### C.2 — Evaluator

```
Task tool:
  subagent_type: harness-evaluator
  description: "Evaluate iteration <iteration> for run <run_id>"
  prompt: "run_id=<run_id>, iteration=<iteration>.
           Read the contract and run each channel exactly once,
           per the per-iteration channel cache rule.
           Write the verdict to
           ~/.cache/memory-cloud-harness/runs/<run_id>/verdict-<iteration>.json
           and return the summary block."
```

### C.3 — Continuation check

After the Evaluator returns, read `verdict-<iteration>.json`:

- If `summary.iteration_complete == true` → break and proceed to Phase D
- Otherwise:
  - Increment `iteration`
  - **Budget breakers** (any one trips → abort with `outcome: failed`):
    - `iteration > max_iterations` — Generator did not converge
    - `claude_usage.input_tokens > contract.budget.max_input_tokens_per_run`
    - `five_hour.used_percentage > contract.budget.abort_if_five_hour_pct_above`
  - **Context pressure** (does NOT abort, only resets):
    - `context_pct > contract.budget.clear_if_context_pct_above` — clear
      and restart from `contract.json` + most recent `verdict-*.json`
      (park-and-resume; counts as the same `run_id`)

When the loop exits cleanly via `iteration_complete`, proceed to Phase D.

## Phase D — Quality

Run the existing project quality command:

```
/quality
```

This executes lint, type-check, frontend tests. Any failure → return to
Phase C with `iteration` incremented and a note appended to the next
verdict explaining the failing check. This handles the case where the
contract's channels passed but a non-channel quality concern slipped
through.

Then run `/self-review`. Any `[C]` finding → return to Phase C the same way.

## Phase E — gate2 (pre-PR review)

Read `contract.gates.gate2_pre_pr_review`. If `enabled: false`, skip and
proceed to Phase F. Otherwise dispatch reviewers exactly as in Phase B.
Loop-back on `[C]` returns to Phase C, not Phase A — gate2 challenges
implementation, not contract.

## Phase F — PR

```
gh pr create --base main --head <current-branch> \
  --title "<conventional commit subject> (#<issue>)" \
  --body "Closes #<issue>

  ...

  Generated by /harness-loop run_id=<run_id>"
```

Capture the PR URL.

## Phase G — gate3 (release audit)

Read `contract.gates.gate3_release_audit`. If `enabled: false`, skip and
proceed to Persist. Otherwise dispatch the audit reviewer
(`claude-c-suite:audit` per the rule file's default). Loop-back on `[C]`
returns to Phase D — release audit challenges quality + final delta, not
the inner loop.

## Persist — run record

Regardless of outcome (`merged`, `failed`, `aborted`, `escalated`), write
one record via `/kagura-memory:remember` matching the **Run record schema**
in the rule file:

```
remember(
  context_id="kagura-dev",
  summary="<run_id> [area:...] <issue title> (#<issue>)",
  content="Run record for harness run <run_id>. See details for full schema.",
  type="harness-run",
  importance=0.7,
  tags=["harness", "area:<a>", "outcome:<outcome>", "issue:<issue>"],
  details=<run record JSON matching the rule file's schema>
)
```

The `outcome` enum is exactly: `merged` | `failed` | `aborted` | `escalated`.
Mapping:

- `merged` — gates all passed and `gh pr merge` succeeded (or PR was opened
  successfully under the deferred-merge plan and outcome is treated as
  `merged` from the command's perspective)
- `failed` — Generator↔Evaluator hit `max_iterations` without converging
- `aborted` — pre-flight or budget breaker tripped
- `escalated` — a gate returned `[C]` after the loop-back limit, or a
  subagent escalated to the user

## Output format (stdout summary)

After persisting the run record, output this summary and stop:

```
## Harness loop — run <run_id>

**Issue**: #<issue> <title>
**Branch**: <branch>
**Outcome**: <outcome>
**PR**: <pr_url or "not created">

### Phase timings
- Plan:           <s>s
- gate1:          <s>s (skipped | <reviewers>)
- Generator×<N>:  <s>s
- Evaluator×<N>:  <s>s
- Quality:        <s>s
- gate2:          <s>s (skipped | <reviewers>)
- gate3:          <s>s (skipped | audit)

### Final verdict
- Contracts: <pass>/<total> passed at iteration <iteration>
- Promoted to regression: <N>
- [C] findings: <N>  [W] findings: <N>

### Run record
Persisted as memory_id=<id> (type=harness-run, importance=0.7)

### Cache directory
~/.cache/memory-cloud-harness/runs/<run_id>/
  contract.json
  verdict-1.json
  verdict-2.json
  ...
```

## Forbidden patterns

Mirror of the rule file's Forbidden patterns section, applied to the
orchestrator specifically:

- **No skipping pre-flight.** All four checks run before any subagent is
  spawned. Skipping is how runs leak past the rate limiter.
- **No editing the contract during the inner loop.** The contract is
  Planner-owned and is frozen at the end of Phase A. If the inner loop
  reveals a contract bug, that is an `[I]` observation for the next run —
  not an excuse to mutate `contracts[]`.
- **No manual exit-code overrides at the Evaluator boundary.** The
  Evaluator's `status` is binding. If you disagree, the loop continues —
  you don't get to call `iteration_complete` yourself.
- **Gates run exactly once per role per run.** gate1 fires once after
  Phase A, gate2 once after Phase D, gate3 once after Phase F. A gate's
  `[C]` finding loops back to the appropriate phase, but the gate itself
  does not re-run inside the loop.
- **No silent budget overrides.** If `max_iterations` would be exceeded,
  abort with `outcome: failed` — do not bump it inline. Adjusting the
  budget is the user's call, not the orchestrator's.
- **Park-and-resume preserves run_id.** When context pressure forces a
  reset, the new context picks up under the same `run_id`. Reusing IDs
  across distinct runs is forbidden, but the same run resuming from cache
  is the whole point of the cache directory.
- **Run record is mandatory.** Even on `aborted` and `escalated` outcomes,
  the run record gets persisted. Without it, future Planners cannot recall
  prior failure modes.

## When to escalate to the user

Stop and ask the user before continuing if any of these hold:

- Pre-flight passed but Phase A's Planner escalated (no extractable
  acceptance criteria, areas empty, etc.)
- gate1 reviewers disagree with each other in a way that cannot be
  resolved by re-planning
- Inner loop has looped back from gate2 more than twice — implementation
  is structurally stuck
- Phase 4 Generator agent is not present in the repo and the user did not
  pass `--dry-run`

Escalation means: write the run record with `outcome: escalated`, print a
single-block reason, and exit without continuing. The user decides whether
to relax the contract, swap reviewers, or invoke `/harness-loop --resume`
after manual intervention.

## References

- `.claude/rules/harness-contract.md` — authoritative schema (gates, budget, run record, outcome enum)
- `.claude/agents/harness-planner.md` — Phase A subagent (lands via PR #253)
- `.claude/agents/harness-evaluator.md` — Phase C.2 subagent (lands via PR #257)
- `.claude/agents/harness-generator.md` — Phase C.1 subagent (Phase 4 — separate PR cycle, not yet authored)
- `.claude/commands/quality.md` — Phase D quality gate
- `.claude/commands/self-review.md` — Phase D self-review gate
- `~/.claude/statusline-command.sh` — pre-flight 5-hour usage probe and `claude_usage` field source
- `CLAUDE.md` — `kagura-dev` context_id and memory-first development posture
