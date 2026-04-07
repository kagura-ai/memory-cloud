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

The canonical runtime version is `APP_VERSION` in `backend/src/config/constants.py`. Both `backend/src/api/main.py` and `backend/src/mcp_server/transport.py` `import APP_VERSION from config.constants`, so they pick up the bump automatically — do NOT edit them directly (it would break the single-source-of-truth pattern).

- `backend/pyproject.toml` — `version = "X.Y.Z"` (Python package metadata)
- `backend/src/config/constants.py` — `APP_VERSION = "X.Y.Z"` (canonical runtime source — drives `/api/v1/info`, `/api/v1/system/telemetry`, MCP `serverInfo.version`, FastAPI OpenAPI `version`)
- `backend/src/__init__.py` — `__version__ = "X.Y.Z"` (compat alias; must stay in sync with `APP_VERSION`)
- `frontend/package.json` — `"version": "X.Y.Z"`
- `frontend/package-lock.json` — run `cd frontend && npm install` to sync lock file

### 5. Commit and tag

```bash
git add backend/pyproject.toml backend/src/config/constants.py backend/src/__init__.py frontend/package.json frontend/package-lock.json
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

**Breaking changes check**: If any merged PR contains breaking API/MCP changes (field renames, tool removals, endpoint changes), add a `## Migration` section at the top of the release notes with:
- What changed (before → after)
- What clients/users need to update
- Example of the new usage

### 8. Upload coverage

```bash
make coverage-upload
```

This runs unit tests with coverage and uploads to Codecov.
Requires `CODECOV_TOKEN` environment variable (set in `.env.local` or export manually).

> **⚠ Known issue (#241)**: As of v0.9.0, `make coverage-upload` is broken — `codecovcli` requires `--sha`/`--slug`/`--git-service` and the pytest invocation uses `-x` which masks fresh coverage data. Until #241 lands, run manually after the tag is pushed:
> ```bash
> cd backend && pytest tests/api/ tests/auth/ tests/smoke/ tests/neural/test_hebbian.py \
>   --cov=src --cov-report=xml --cov-report=term-missing || true
> codecovcli upload-process --token $CODECOV_TOKEN -f coverage.xml \
>   --sha $(git rev-parse HEAD) \
>   --slug kagura-ai/memory-cloud \
>   --git-service github
> ```

### 9. Report

Print the new version, link to the GitHub Release, and the Actions run.
