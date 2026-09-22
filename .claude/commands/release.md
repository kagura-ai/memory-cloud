---
description: Release via a release branch, PR, squash merge, annotated tag and GitHub Release
arguments:
  - name: level
    description: "patch, minor, or major"
    required: true
---

Release Kagura Memory Cloud with a version bump.

`main` is protected: pull request required (enforced for admins), linear history,
no force pushes. A release is therefore a normal squash-merged PR, and the tag is
created **after** the merge, on the commit that landed on `main`. Never commit on
`main`, and never push a tag made before the squash — it would point at a commit
that is not on `main` (a dangling tag and release; it happened once and had to be
deleted and redone).

This command is the whole ceremony for this repo. `/gh-issue-driven:tag` is not run
here end-to-end: it commits and pushes on `main`, bumps only the plugin manifests
(and aborts on the `version` field that `.claude-plugin/marketplace.json` does not
have), so it would leave the seven version files below out of lockstep. At most
use it in `dry-run` to draft notes.

## Prerequisites

Before releasing, ensure:
1. `/quality` has been run and passes
2. Current branch is `main`, clean, and equal to `origin/main`
3. Every PR for the milestone is merged and CI on `main` is green

## Steps

### 1. Validate preconditions

- Argument: `$ARGUMENTS` (must be `patch`, `minor`, or `major`)
- `git branch --show-current` is `main`; `git status --porcelain` is empty. Abort otherwise.
- `git fetch origin` and `git rev-parse HEAD origin/main` print the same SHA.
- The milestone for the new version exists and has no open issues:
  `gh api repos/{owner}/{repo}/milestones --jq '.[] | select(.title == "vX.Y.Z") | {number, open_issues}'`
  must show `open_issues: 0`. Abort if issues are open (finish or move them first).
- The tag does not exist yet: `git ls-remote --tags origin refs/tags/vX.Y.Z` prints nothing.

### 2. Read current version

Read `APP_VERSION` from `backend/src/config/constants.py` (the canonical source; the
other files must already agree — `backend/tests/test_release_version_lockstep.py`
guards that).

### 3. Calculate new version

Apply SemVer bump:
- `patch`: 0.1.0 → 0.1.1
- `minor`: 0.1.0 → 0.2.0
- `major`: 0.1.0 → 1.0.0

### 4. Create the release branch

```bash
git switch -c release/vX.Y.Z
```

All edits below happen on this branch.

### 5. Update version in all locations

The canonical runtime version is `APP_VERSION` in `backend/src/config/constants.py`. Both `backend/src/api/main.py` and `backend/src/mcp_server/transport.py` `import APP_VERSION from config.constants`, so they pick up the bump automatically — do NOT edit them directly (it would break the single-source-of-truth pattern).

- `backend/pyproject.toml` — `version = "X.Y.Z"` (Python package metadata)
- `backend/src/config/constants.py` — `APP_VERSION = "X.Y.Z"` (canonical runtime source — drives `/api/v1/system/info`, `/api/v1/system/telemetry`, MCP `serverInfo.version`, FastAPI OpenAPI `version`)
- `backend/src/__init__.py` — `__version__ = "X.Y.Z"` (compat alias; must stay in sync with `APP_VERSION`)
- `frontend/package.json` — `"version": "X.Y.Z"`
- `frontend/package-lock.json` — run `cd frontend && npm install` to sync lock file (both the root `version` and `packages[""].version`)
- `.claude-plugin/plugin.json` — `"version": "X.Y.Z"` (kagura-memory Claude Code plugin manifest; kept in lockstep so marketplace consumers see the same version as the backend)
- `plugins/kagura-memory/.codex-plugin/plugin.json` — `"version": "X.Y.Z"` (kagura-memory Codex plugin manifest; kept in lockstep with the Claude plugin manifest)

### 6. Add the CHANGELOG entry

Prepend a new entry to `CHANGELOG.md`, above the previous release, dated in **UTC**
(`date -u +%F`). A local-time date written around midnight is one day ahead of the
merge timestamp GitHub shows, and reviewers flag it as a future date.

```markdown
## [vX.Y.Z](https://github.com/kagura-ai/memory-cloud/releases/tag/vX.Y.Z) — YYYY-MM-DD

<One or two sentences: the theme of the release.>

### Added
- **<What>** ([#N](https://github.com/kagura-ai/memory-cloud/issues/N)): <user-visible effect>.

### Changed
### Fixed
### Notes
- Migration / environment variables / operator action, or "No migration, no new environment variables, no operator action."
```

- Source the bullets from `git log --oneline vPREV..HEAD` (every merged PR names its issue).
- One issue link per bullet; drop empty sections.
- **Breaking changes check**: if any merged PR contains breaking API/MCP changes (field renames, tool removals, endpoint changes), add a `### Migration` subsection with what changed (before → after), what clients/users need to update, and an example of the new usage.

### 7. Run the lockstep guards

Run from the repo root (the subshells keep the cwd there for the `git add` paths in step 8):

```bash
(cd backend && pytest tests/test_release_version_lockstep.py tests/test_codex_plugin_manifest.py -q)
(cd frontend && npx vitest run src/lib/version.test.ts)
```

Fix any drift before committing — a missed file fails CI on the PR anyway.

### 8. Commit, push the branch, open the PR

```bash
git add backend/pyproject.toml backend/src/config/constants.py backend/src/__init__.py frontend/package.json frontend/package-lock.json .claude-plugin/plugin.json plugins/kagura-memory/.codex-plugin/plugin.json CHANGELOG.md
git commit -m "chore(release): vX.Y.Z"
git push -u origin release/vX.Y.Z
git ls-remote --heads origin release/vX.Y.Z   # confirm the push landed
gh pr create --base main --head release/vX.Y.Z --title "chore(release): vX.Y.Z" --body-file <body.md>
```

PR body shape (same as every release PR):

```markdown
## Summary

Release vX.Y.Z (milestone vX.Y.Z).

- Bumps the seven version files to X.Y.Z (`backend/pyproject.toml`, `backend/src/config/constants.py`, `backend/src/__init__.py`, `frontend/package.json`, `frontend/package-lock.json`, `.claude-plugin/plugin.json`, `plugins/kagura-memory/.codex-plugin/plugin.json`).
- Adds the vX.Y.Z entry to `CHANGELOG.md` (dated in UTC).

Included since vPREV:

- #<PR> <subject> (#<issue>)   ← one line per entry of `git log --oneline vPREV..HEAD`

## Behaviour changes

None in this PR. See the CHANGELOG entry for the release as a whole.

## Tests

Version strings only; CI runs the full suite.
```

Request exactly one Copilot review round (`gh pr edit --add-reviewer` does not accept the bot):

```bash
gh api -X POST repos/{owner}/{repo}/pulls/<N>/requested_reviewers -f 'reviewers[]=copilot-pull-request-reviewer[bot]'
```

### 9. Resolve review, wait for CI, squash merge

- Address every Copilot finding (fix or reply with the reason). Push fixes as new commits — do not amend and force-push right before merging.
- Wait for CI: `gh pr checks <N> --watch`.
- Confirm the PR head is the commit you pushed last: `gh pr view <N> --json headRefOid` equals `git rev-parse HEAD`. (A squash merge issued right after a push can merge the previous head.)

```bash
gh pr merge <N> --squash --delete-branch
gh pr view <N> --json state,mergeCommit               # MERGED + the squash commit's oid
```

`gh pr merge` also switches the local checkout to `main` and deletes the local
branch. In a linked worktree (where `main` is checked out elsewhere) that local
step exits 1 **after** the merge has landed, so check the PR state rather than the
exit code, then move to the checkout that has `main` — the first line of
`git worktree list` — before continuing. Steps 10–13 run there, on `main`:

```bash
git fetch origin && git merge --ff-only origin/main   # not `git pull`: it can fail with "multiple branches"
[ "$(git rev-parse HEAD)" = "$(gh pr view <N> --json mergeCommit --jq .mergeCommit.oid)" ] || echo "HEAD is not the squash-merge commit — stop"
```

### 10. Tag the merge commit

The tag is annotated and points at the squash-merge commit now at the tip of `main` — never at the local release-branch commit.

```bash
git log --oneline -1                                   # chore(release): vX.Y.Z (#N)
git tag -a vX.Y.Z -m "vX.Y.Z — <one-line theme>" "$(git rev-parse HEAD)"
git push origin vX.Y.Z
git ls-remote --tags origin refs/tags/vX.Y.Z           # confirm the tag landed
```

The tag push triggers the `push: tags: ["v*"]` CI run in `.github/workflows/ci.yml`.

### 11. Create the GitHub Release

Derive `summary.md` from the CHANGELOG entry: a `## Summary` section (theme + one bullet per headline change, with issue numbers) and a `## Migration` section (`None.` plus what is additive, or the migration steps). `--generate-notes` appends `## What's Changed` and the compare link after it.

```bash
gh release create vX.Y.Z --verify-tag --title "vX.Y.Z" --generate-notes --notes-file summary.md
```

`--verify-tag` refuses to create the release if the tag is not on the remote yet.

### 12. Close the milestone

```bash
gh api -X PATCH repos/{owner}/{repo}/milestones/<number> -f state=closed
```

(`<number>` from the precondition query in step 1.)

### 13. Upload coverage

```bash
make coverage-upload
```

Runs the unit tests with coverage and uploads to Codecov with `--sha $(git rev-parse HEAD)`,
so the checkout must be at the tagged commit or the report attaches to the wrong SHA. Check first:

```bash
[ "$(git rev-parse HEAD)" = "$(git rev-parse 'vX.Y.Z^{commit}')" ] || echo "HEAD is not vX.Y.Z — stop"
```

Requires `CODECOV_TOKEN` (read from `.env.local`, or export it).

### 14. Report

Print:
- the new version and the GitHub Release URL,
- the tag-triggered CI run and its conclusion, matched by the tagged commit's SHA:
  `gh run list --workflow ci.yml --event push --commit "$(git rev-parse 'vX.Y.Z^{commit}')" --limit 1`
  (`--branch vX.Y.Z` also finds it — a tag push is recorded with the tag name as `head_branch` — but the SHA match cannot pick up a branch that happens to share the name),
- the Codecov status on the merge commit: `gh api repos/{owner}/{repo}/commits/$(git rev-parse HEAD)/status --jq '.statuses[] | select(.context == "codecov/patch") | .state'`.

## Recovery

A checkout left with a local `chore(release)` commit and a lightweight tag on `main`
(the pre-#1625 procedure) is reset with:

```bash
git tag -d vX.Y.Z
git reset --hard origin/main
```

Then start again from step 4.
