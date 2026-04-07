---
paths:
  - "**"
---

# Harness Contract Schema

Shared language for the Planner / Generator / Evaluator harness. All three
agents read and write JSON conforming to the schemas below. If this file and
an agent disagree, **this file wins** — agents must be updated to match.

## Project assumptions (memory-cloud specific)

This schema is intentionally not generic. It hardcodes memory-cloud conventions
so the harness stays fast and concrete. If harness is ever extracted to a
plugin, these are the values to abstract.

- **Test runners**: `make test-local`, `make test-integration`,
  `make test-e2e`, `make test-smoke`, `make test-neural`,
  `make test-frontend` (which wraps `cd frontend && npm test`)
- **Source areas**: `backend/src/api/`, `backend/src/auth/`,
  `backend/src/mcp_server/`, `backend/src/neural/`, `backend/src/db/`,
  `backend/src/models/`, `backend/src/services/`, `backend/alembic/`,
  `frontend/`
- **Memory context**: `kagura-dev` (see CLAUDE.md)
- **Branch protection**: `main`, squash merge, conventional commits
- **Budget thresholds**: 5h rate-limit abort at 80%, context clear at 75%

## Purpose

The contract is the single source of truth for a harness run. The Planner
writes it before any implementation starts. The Generator reads it to know
what to build. The Evaluator reads it to know what to verify, and its verdict
is structured against the contract ID-for-ID.

Without a frozen contract, the Generator and Evaluator drift — the Generator
builds what it thinks was asked, the Evaluator judges by what it thinks is
good, and neither answers "did we do what the issue requested?". The contract
closes that gap.

## Storage

- **In-flight** (during a run): `~/.cache/memory-cloud-harness/runs/<run-id>/contract.json`
  as a plain file. Generator and Evaluator read it directly — no `recall`
  round trips in the inner loop. Out-of-tree by design: no gitignore entry,
  no accidental commits, and run state survives reboots so park-and-resume
  works when the 5-hour budget breaker trips mid-run.
- **Post-run** (on completion): persisted via `/kagura-memory:remember` with
  `type=harness-contract`, `importance=0.8`, and the full contract JSON in
  `details`. See the **Memory type namespace** section for the exact call
  shape.
- **Run ID format**: `hr-YYYYMMDD-NNN` (date + daily counter). Unique across
  runs; reused IDs are forbidden.

## Top-level contract schema

```json
{
  "schema_version": "1",
  "run_id": "hr-20260408-001",
  "issue": 234,
  "issue_title": "Enforce cross-workspace RBAC on context endpoints",
  "branch": "234-feat/context-rbac",
  "created_at": "2026-04-08T10:15:00Z",

  "areas": ["api", "db"],
  "detected_from": [
    "backend/src/api/contexts.py",
    "backend/alembic/versions/20260408_add_workspace_index.py"
  ],

  "contracts": [
    {
      "id": "C-234-01",
      "statement": "POST /contexts rejects cross-workspace context_id with 403",
      "channel": "make test-integration",
      "evidence_target": "backend/tests/api/test_contexts.py::test_rbac_isolation",
      "promotion_candidate": true,
      "reason": "RBAC bypass would warrant a new issue — re-occurrence guard mandatory"
    },
    {
      "id": "C-234-02",
      "statement": "alembic upgrade head → downgrade -1 → upgrade head is idempotent",
      "channel": "make test-integration",
      "evidence_target": "backend/tests/integration/test_alembic_migrations.py",
      "promotion_candidate": true,
      "reason": "Migration regressions are destructive; roundtrip must be enforced"
    }
  ],

  "gates": {
    "gate1_planner_review": {
      "enabled": true,
      "reviewers": ["claude-c-suite:cto", "claude-phd-panel:db"]
    },
    "gate2_pre_pr_review": {
      "enabled": true,
      "reviewers": ["claude-c-suite:cso"]
    },
    "gate3_release_audit": {
      "enabled": false,
      "reviewers": []
    }
  },

  "budget": {
    "max_iterations": 5,
    "max_input_tokens_per_run": 250000,
    "abort_if_five_hour_pct_above": 80,
    "clear_if_context_pct_above": 75
  }
}
```

### Field rules

- `schema_version` — string, currently `"1"`. Bump on any breaking change.
- `run_id` — `hr-<date>-<nnn>`. Planner assigns at run start.
- `issue` — integer GitHub issue number. Required.
- `areas` — array derived from `detected_from` via the area detection table
  below. Never empty; defaults to `["lib"]` if no match.
- `contracts[].id` — `C-<issue>-<NN>`, zero-padded to 2 digits, unique within
  the issue AND across runs for the same issue.
- `contracts[].statement` — one sentence, written so a pass/fail is decidable
  by running `channel`. No "should feel right" language.
- `contracts[].channel` — must be one of the **Channel enum** values below.
- `contracts[].evidence_target` — the exact test path, file, or URL the
  Evaluator will use. `null` only allowed for `self-review` channel.
- `contracts[].promotion_candidate` — Planner's initial guess. Evaluator
  re-scores via the **promotion test** below and may downgrade.
- `gates.*` — each gate is an object with two fields: `enabled` (boolean)
  and `reviewers` (array of role slugs). When `enabled` is `false`,
  `reviewers` must be `[]`. This uniform shape lets the harness parse all
  three gates without type branching.

## Channel enum

`channel` is **not** a free-form string. Exactly these values are accepted:

Each row lists **when Planner picks it** (trigger), **what the underlying
target actually runs** (real scope, verified against `Makefile`), and the
**Evaluator invocation**. Trigger and scope can differ — `make test-local`
runs the entire backend suite, but the Planner picks it as the default for
work that has no narrower channel.

| channel                 | When Planner picks it                                                                  | What it runs (real scope)                                                                          | Evaluator invocation                |
| ----------------------- | -------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- | ----------------------------------- |
| `make test-local`       | Default when no narrower channel matches                                               | `cd backend && pytest -v --maxfail=5` — full backend suite (all of `backend/tests/`)               | `make test-local`                   |
| `make test-integration` | `backend/alembic/`, `backend/src/api/`, `backend/src/auth/`, `backend/src/db/` in diff | `cd backend && pytest tests/integration/ -v --timeout=120` — only `backend/tests/integration/`     | `make test-integration`             |
| `make test-neural`      | `backend/src/neural/` in diff                                                          | `docker exec kagura-api python -m pytest tests/neural/ -v` — only `backend/tests/neural/`          | `make test-neural`                  |
| `make test-smoke`       | Health, auth, well-known paths                                                         | `cd backend && pytest tests/smoke/ -v --timeout=30` — only `backend/tests/smoke/`                  | `make test-smoke`                   |
| `make test-e2e`         | Cross-service flow (memory lifecycle, rate limit)                                      | `cd backend && pytest tests/e2e/ -v --timeout=60` — only `backend/tests/e2e/`                      | `make test-e2e`                     |
| `make test-frontend`    | `frontend/**` unit tests                                                               | `cd frontend && npm test` — Vitest suite under `frontend/src/**/*.test.{ts,tsx}`                   | `make test-frontend`                |
| `playwright-mcp`        | `frontend/**` UI behavior verification                                                 | Playwright MCP browser tools driving the running frontend                                          | Playwright MCP browser tools        |
| `mcp-live`              | `backend/src/mcp_server/` or `backend/tests/mcp_server/` changes                       | Live MCP tool calls against the running server                                                     | `kagura-memory` MCP tool real calls |
| `self-review`           | Static code review, no runtime verification                                            | `/self-review` slash command — no test execution                                                   | `/self-review` invocation           |

The enum mixes **Makefile-backed channels** (the `make test-*` rows) and
**non-Makefile channels** (`playwright-mcp`, `mcp-live`, `self-review`).
Makefile-backed channels are validated by CI against `Makefile` targets.
Non-Makefile channels are validated by the Evaluator at run time: they
invoke MCP tools or slash commands directly and bind `status` to the
underlying tool's exit code (for `playwright-mcp`, the Playwright step
return; for `mcp-live`, the MCP tool response status; for `self-review`,
the presence of any `[C]` finding → `fail`). Planners must never invent a
channel not in this table.

Planners may assign multiple contracts to the same channel. Each contract
invokes the channel independently; the Evaluator caches channel results per
iteration so identical channels across contracts run once per iteration.

## Area detection rules

Path-based, label-independent. The Planner runs this mapping against
`git diff --name-only main...HEAD` (or issue-linked file hints if no branch
exists yet):

```
backend/src/api/                         → api
backend/src/auth/                        → auth
backend/src/mcp_server/                  → mcp
backend/src/neural/                      → neural
backend/alembic/                         → db
backend/src/db/, backend/src/models/     → db
backend/src/services/                    → services
frontend/                                → frontend
backend/tests/, frontend/**/__tests__/   → test
.claude/, docs/, .github/                → meta
(no match)                               → lib
```

Multi-match is allowed and expected. `areas: ["api", "db"]` is normal for a
feature that touches an endpoint plus its schema. An empty array is a bug —
default to `["lib"]` instead.

## Evaluator verdict schema

Each iteration, the Evaluator writes one verdict document to
`~/.cache/memory-cloud-harness/runs/<run-id>/verdict-<iteration>.json`:

```json
{
  "run_id": "hr-20260408-001",
  "iteration": 2,
  "verdicts": [
    {
      "contract_id": "C-234-01",
      "status": "pass",
      "channel_invoked": "make test-integration",
      "evidence": "backend/tests/api/test_contexts.py::test_rbac_isolation PASSED",
      "exit_code": 0,
      "fix_hint": null,
      "promotion_candidate": true
    },
    {
      "contract_id": "C-234-02",
      "status": "fail",
      "channel_invoked": "make test-integration",
      "evidence": "alembic.util.exc.CommandError: Can't locate revision identified by '...'",
      "exit_code": 1,
      "fix_hint": "down_revision pointer broken in 20260408_add_workspace_index.py — should reference previous head",
      "promotion_candidate": true
    }
  ],
  "summary": {
    "passed": 1,
    "failed": 1,
    "iteration_complete": false
  }
}
```

### Verdict rules (these are the anti-softening rules)

- **`status` is bound to `exit_code`, not to judgment.** Exit 0 → `pass`.
  Anything else → `fail`. The Evaluator is not allowed to argue that a
  non-zero exit was "not really a failure". This prevents the Anthropic-team
  problem where Evaluators talk themselves into passing broken contracts.
- **`evidence` is raw tool output**, not a summary. Copy the failing line
  verbatim (trimmed to ~500 chars if long).
- **`fix_hint` is machine-actionable**, not a critique. A good `fix_hint`
  tells the Generator which file and what change. A bad `fix_hint` says
  "the test failed, consider reviewing the logic".
- **The Evaluator does not invent new contracts.** If it discovers behavior
  that should be tested but is not in the contract, it records an `[I]`
  observation in the run record for the Planner's next pass — not in the
  verdict.
- **`summary.iteration_complete`** is `true` only when all contracts have
  `status: pass`. Otherwise the Generator is recalled.

## Promotion test

A contract is promoted to the permanent regression suite (i.e., an actual
test file is committed) only if `promotion_candidate` is `true` AND the
Evaluator re-scores it with these three questions, all answered yes:

1. If this behavior breaks in the future, would a separate issue be opened?
2. Does `recall(context_id="kagura-dev", query=<statement>, filters={"type": "troubleshooting"})`
   return any prior occurrence of this bug? (A hit is a strong yes —
   re-occurrence mandates a test.)
3. Is the cost of maintaining the test lower than the cost of the bug
   recurring?

One `no` → the Evaluator sets `promotion_candidate: false` in the verdict
and the contract is not promoted. The Planner's initial guess is advisory
only; the Evaluator has the final call.

## Run record schema

On run termination (merged, failed, aborted, or escalated), the harness
writes one record via `/kagura-memory:remember`:

```json
{
  "run_id": "hr-20260408-001",
  "issue": 234,
  "started_at": "2026-04-08T10:15:00Z",
  "ended_at":   "2026-04-08T10:47:22Z",
  "wall_clock_sec": 1942,
  "outcome": "merged",

  "phases": {
    "planner":   {"duration_sec": 45,   "status": "ok"},
    "generator": {"duration_sec": 1200, "iterations": 3, "status": "ok"},
    "evaluator": {"duration_sec": 480,  "iterations": 3, "status": "pass"},
    "gate1": {"role": "claude-c-suite:cto", "verdict": "pass", "warnings": 1},
    "gate2": {"role": "claude-c-suite:cso", "verdict": "pass", "warnings": 0},
    "gate3": {"skipped": true}
  },

  "claude_usage": {
    "model": "claude-opus-4-6[1m]",
    "input_tokens": 124500,
    "output_tokens": 18200,
    "cache_read_tokens": 890000,
    "context_peak_pct": 62,
    "five_hour_delta_pct": 14,
    "seven_day_delta_pct": 3
  },

  "evaluator_findings": {
    "critical": 0,
    "warning": 2,
    "promoted_to_regression": 1
  },
  "regression_added": [
    "backend/tests/api/test_contexts.py::test_rbac_isolation"
  ],
  "pr_url": "https://github.com/.../pull/245"
}
```

`outcome` enum: `merged` | `failed` | `aborted` | `escalated`.

- `merged`     — PR created, all gates passed, PR merged
- `failed`     — Generator↔Evaluator hit `max_iterations` without converging
- `aborted`    — Budget breaker tripped (rate limit, context, tokens)
- `escalated`  — Human intervention required (gate rejected, contract ambiguous)

`claude_usage` field names mirror `~/.claude/statusline-command.sh` output
exactly so the harness can parse the statusline JSON without remapping.

## Memory type namespace

Four new `type` values, all prefixed `harness-` to avoid collisions with
existing types (`pattern`, `troubleshooting`, `decision`, `learning`,
`bug-fix`, `code`, `note`, `feature`, `config`). The existing-type list was
collected from `claude-skills/remember.md` and `claude-skills/recall.md`,
which document the conventions used by the `/kagura-memory:*` skills.

| type                  | When written                                    | importance | Required tags                                            |
| --------------------- | ----------------------------------------------- | ---------- | -------------------------------------------------------- |
| `harness-contract`    | Run completion — the frozen final contract      | 0.8        | `harness`, `area:<a>`, `issue:<n>`                       |
| `harness-run`         | Run completion — the run record above           | 0.7        | `harness`, `area:<a>`, `outcome:<o>`, `issue:<n>`        |
| `harness-test-result` | Evaluator verdict when it has reference value   | 0.5        | `harness`, `area:<a>`, `issue:<n>`                       |
| `harness-decision`    | `[W]` warnings dismissed by the Generator       | 0.7        | `harness`, `area:<a>`, `issue:<n>`, `decision:dismissed` |

Example `remember` call for the final contract:

```json
{
  "context_id": "kagura-dev",
  "summary": "hr-20260408-001 [area:api,db] RBAC on context endpoints (#234)",
  "content": "Final contract for run hr-20260408-001. See details for schema.",
  "type": "harness-contract",
  "importance": 0.8,
  "tags": ["harness", "area:api", "area:db", "issue:234"],
  "details": {}
}
```

In practice, `details` is populated with the full contract JSON at write
time. It is shown empty above so the example parses as valid JSON; the
linter in CI rejects block comments inside JSON code fences.

Bugs found by the Evaluator that include a root-cause fix use the existing
`type=troubleshooting` (importance 0.9) — this is not a new type, just reuse
of the existing convention so prior knowledge queries naturally find
harness-discovered bugs.

## Forbidden patterns

- **No "vibes" contracts.** Every `statement` must be verifiable by running
  `channel` and inspecting `exit_code`. If you can't write the verification
  as a shell command, the contract is not ready.
- **No contract ID reuse.** `C-234-01` belongs to issue 234 forever. Even on
  a re-run of the same issue, start from `C-234-<next unused NN>`.
- **The Evaluator never edits `contracts[]`.** Contracts are Planner-owned.
  The Evaluator only writes verdicts.
- **`status: fail` carries no opinion.** `evidence` is raw output,
  `fix_hint` is machine-actionable. No "I think maybe...".
- **Gates don't enter the inner loop.** gate1 fires once after Planner,
  gate2 once after Evaluator passes, gate3 only on release PRs. A gate
  finding `[C]` sends control back to the appropriate phase, but the gate
  itself runs only once per role per run.
- **Budget breakers are non-negotiable.** If `five_hour_pct > 80` at
  pre-flight, abort. If `context_pct > 75` mid-flight, clear and restart
  from the contract file. No overrides without human confirmation.

## References

- `.claude/commands/issue-start.md` — Planner's entry point for branch + recall
- `.claude/commands/workflow.md` — Generator's state transition table
- `.claude/commands/self-review.md` — `self-review` channel implementation
- `.claude/commands/quality.md` — Generator's final in-loop check
- `.claude/rules/development-workflow.md` — Commit and PR discipline this harness must respect
- `Makefile` — Authoritative source for `test-*` channel targets
- `~/.claude/statusline-command.sh` — `claude_usage` field names and rate-limit breakers
- `CLAUDE.md` — `kagura-dev` context_id and memory-first development posture
