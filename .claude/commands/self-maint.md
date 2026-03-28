---
description: Audit and maintain .claude/ configuration (commands, agents, rules, hooks, settings)
---

Perform a full audit of the `.claude/` directory against the current codebase state. Report all findings, then ask which fixes to apply.

## Audit Steps

### 1. Rules Audit (.claude/rules/)

Read each rule file (`backend.md`, `frontend.md`, `security.md`):

- Verify that referenced imports, packages, and patterns still exist in the codebase
- Check if new patterns or conventions have emerged in the code that rules don't cover
- Verify that forbidden patterns listed in rules are not present in the code
- Flag any stale or incorrect rules

### 2. Commands Audit (.claude/commands/)

Read each command file:

- Verify that referenced CLI commands work (make targets, npm scripts, pytest commands)
- Check that referenced MCP tools and parameters match the current API
- Verify file paths and tool references are still valid
- Flag outdated instructions or broken references

### 3. Agents Audit (.claude/agents/)

Read each agent definition:

- Verify referenced file paths, test patterns, and tech stack descriptions are current
- Check that tool restrictions and model specifications are appropriate
- Verify consistency with current project structure

### 4. Hooks Audit (settings.json)

Read `.claude/settings.json`:

- Verify that hook scripts exist and are executable
- Check that file pattern matchers cover current file types in the project
- Verify that protection rules (blocked files/patterns) are still relevant
- Check that the secret detection patterns are comprehensive

### 5. Permissions Audit (settings.json)

Review the `permissions.allow` list:

- Check if any allowed commands reference tools not installed in the project
- Check if any commonly needed commands are missing from the allowlist
- Verify deny list is appropriate

### 6. Documentation Audit (docs/)

- Verify all docs are in English (no Japanese or mixed-language files)
- Check for broken internal links between docs
- Verify docs referenced from README.md, CLAUDE.md, and source code exist
- Flag stale or deprecated documentation
- Ensure `docs/` structure is flat (no `en/` or `ja/` subdirectories)

### 7. CLAUDE.md Consistency Check

- Verify that CLAUDE.md references match actual `.claude/` contents
- Check that listed commands, agents, and rules exist
- Flag any drift between CLAUDE.md and the actual configuration

## Output Format

Present findings as a table:

| # | Severity | File | Issue | Proposed Fix |
|---|----------|------|-------|-------------|
| 1 | Critical | ... | ... | ... |
| 2 | Warning | ... | ... | ... |
| 3 | Info | ... | ... | ... |

**Severity levels:**
- **Critical**: Broken references, security gaps, non-functional commands
- **Warning**: Stale information, missing coverage, inconsistencies
- **Info**: Suggestions for improvement, minor drift

After presenting findings, ask the user which fixes to apply. Do not modify `settings.local.json` (user-specific).
