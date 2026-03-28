---
description: Bump version, commit, tag, and push a release
arguments:
  - name: level
    description: "patch, minor, or major"
    required: true
---

Release Kagura Memory Cloud with a version bump.

## Prerequisites

Before releasing, ensure:
1. `/quality` has been run and passes
2. Current branch is `main`
3. All PRs merged and CI passing

## Steps

### 1. Validate preconditions

- Argument: `$ARGUMENTS` (must be `patch`, `minor`, or `major`)
- Verify current branch is `main` (`git branch --show-current`). Abort if not on main.

### 2. Read current version

Read `backend/pyproject.toml` and extract the version field.

### 3. Calculate new version

Apply SemVer bump:
- `patch`: 0.1.0 → 0.1.1
- `minor`: 0.1.0 → 0.2.0
- `major`: 0.1.0 → 1.0.0

### 4. Update version in all locations

- `backend/pyproject.toml` — `version = "X.Y.Z"`
- `backend/src/api/main.py` — `version="X.Y.Z"` in FastAPI app
- `backend/src/mcp_server/transport.py` — `"version": "X.Y.Z"` in serverInfo
- `frontend/package.json` — `"version": "X.Y.Z"`

### 5. Commit and tag

```bash
git add backend/pyproject.toml backend/src/api/main.py backend/src/mcp_server/transport.py frontend/package.json
git commit -m "chore(release): vX.Y.Z"
git tag vX.Y.Z
```

### 6. Push

```bash
git push && git push --tags
```

### 7. Create GitHub Release with notes

```bash
gh release create vX.Y.Z --generate-notes --title "vX.Y.Z"
```

Review the auto-generated notes. If needed, edit to add a summary section at the top.

### 8. Upload coverage

```bash
make coverage-upload
```

This runs unit tests with coverage and uploads to Codecov (requires `CODECOV_TOKEN` in environment).

### 9. Report

Print the new version, link to the GitHub Release, and the Actions run.
