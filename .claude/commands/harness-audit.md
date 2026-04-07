Run the meta-consistency audit for the Planner / Evaluator / Generator harness.

This is the Phase 7 maintenance check. It catches bug classes that
`make harness-lint` (Phase 6) does not cover — specifically:

- **Path drift** — every repo path referenced in a harness file must
  exist on disk. This catches the class of bugs where an agent prompt
  references `backend/src/<wrong-dir>/` when the real location is
  elsewhere — the exact pattern that consumed 3 review rounds on PR #251
- **Field-name drift** — identifiers like `five_hour_pct` must appear
  in the canonical `.claude/rules/harness-contract.md` (the
  copy-paste-across-3-files class that Copilot caught on PR #257)
- **Channel enum vs Makefile** — every `make test-*` value in the
  Channel enum must be a real Makefile target
- **recall API shape** — every call must include a context_id argument
  and use filters={type: ...} rather than passing type directly as a
  top-level kwarg

## When to run

- **Before merging any harness-related PR** — the stacked sibling PRs
  (#253, #257, #259, #261) should pass audit before the stack merges to
  main
- **Before `/release`** — audit is a release gate
- **Monthly maintenance** — the harness stack evolves; drift creeps in
- **When a sibling PR adds a new harness file** — the new file is
  automatically included in the audit scope

## Steps

1. Run the audit script:

   ```bash
   python3 scripts/harness_audit.py
   ```

2. Read the output. Severity scale:

   - **`[C]` critical** — blocks release. Exit code is 1 if any are
     present. Always fix before proceeding.
   - **`[W]` warning** — non-blocking but should be triaged. These
     indicate drift that has not yet become a real bug but is headed
     there.
   - **`[I]` info** — design smells (missing Forbidden patterns section,
     agent doesn't reference the contract doc). Non-blocking; address
     when convenient.

3. For each `[C]` finding:

   - **Path drift** — run `ls <path>` to confirm the path really does
     not exist, then either fix the reference or mark it as a forward
     reference with a nearby "lands via PR #N" / "Phase N" marker
   - **Field-name drift** — grep the contract doc for the canonical
     field name (e.g., `five_hour_pct` vs `five_hour.used_percentage`)
     and replace across all harness files. Check all sibling PRs —
     this class propagates via copy-paste.
   - **Channel enum drift** — either add the missing Makefile target
     or remove the channel from the Channel enum
   - **`recall()` shape** — rewrite to the canonical form
     `recall(context_id="kagura-dev", query=..., filters={"type": ...})`

4. Re-run the audit until exit code is 0.

## Usage variants

```bash
# Default — run all checks, exit 1 if any [C]
python3 scripts/harness_audit.py

# Verbose — also show [I] findings (useful for maintenance audits)
python3 scripts/harness_audit.py --verbose
```

## Relationship to `make harness-lint`

Phase 6 (`harness-lint`) validates one file at a time: the contract
doc's JSON blocks parse, a given per-run `contract.json` conforms to
the schema. Phase 7 (`harness-audit`) validates **cross-file
consistency** — the set of harness files as a whole agrees with the
authoritative schema. They are complementary, not overlapping.

Run both before merging harness-related work:

```bash
make harness-lint && python3 scripts/harness_audit.py
```

## References

- `scripts/harness_audit.py` — the audit script
- `scripts/harness_contract_lint.py` — the Phase 6 complement
  (lands via PR #261 — reference that PR until both merge to main)
- `.claude/rules/harness-contract.md` — authoritative schema
- Memory: `feedback_verify_paths_with_ls` — the path drift rule
- Memory: troubleshooting memory id `3e26f9d4` — the field-name drift class
