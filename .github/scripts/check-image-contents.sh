#!/usr/bin/env bash
# Checks what a built api or web image carries (#2763).
#
# backend/tests/ci/test_image_contents.py reads the Dockerfile instructions;
# this script runs the built image, so a base image or a dependency that brings
# in a compiler, the test tools or a package manager fails CI as well. Each
# probe is its own `docker run --rm --entrypoint <cmd>`, so no probe goes
# through the image's CMD.
#
# api (the default target, which is what the production compose files build):
#   - none of the dev extra: pytest, fakeredis, playwright do not import;
#     ruff and pyright are not on PATH
#   - no compiler or build tool: gcc, cc, make
#   - the base image's pip, setuptools and wheel are gone (system Python)
#   - uv and uvx are gone
#   - runs as uid 1000; the application imports; alembic and curl (the
#     health check) run
# web (the runner stage):
#   - no package manager: npm, npx, corepack, yarn, yarnpkg
#   - runs as nextjs; server.js is in the working directory
#   - Node's major is the one frontend/Dockerfile names
#
# A check that cannot run (docker fails, the probe exits with anything other
# than its two answers) fails too: an absent tool must be proven absent.
#
# Usage: check-image-contents.sh api|web <image>
# Env:   DOCKER (default docker; bats stubs it, as wait-for-api.sh's CURL),
#        WEB_DOCKERFILE (where the Node major is read; default
#        frontend/Dockerfile of this checkout).
set -euo pipefail

usage="usage: check-image-contents.sh api|web <image>"
kind="${1:?$usage}"
image="${2:?$usage}"
DOCKER="${DOCKER:-docker}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
web_dockerfile="${WEB_DOCKERFILE:-$repo_root/frontend/Dockerfile}"
failures=0

if [ "$kind" != api ] && [ "$kind" != web ]; then
  echo "$usage" >&2
  exit 2
fi

probe() {
  local entrypoint="$1"
  shift
  "$DOCKER" run --rm --entrypoint "$entrypoint" "$image" "$@"
}

pass() { echo "ok: $*"; }

fail() {
  echo "::error::$image: $*"
  failures=$((failures + 1))
}

# The probe's exit status, without tripping set -e. Its output is not needed.
status_of() {
  local rc=0
  probe "$@" > /dev/null 2>&1 || rc=$?
  echo "$rc"
}

# <what> <probe...>: the probe exits 1 when <what> is absent, 0 when present.
expect_absent() {
  local what="$1" rc
  shift
  rc="$(status_of "$@")"
  if [ "$rc" -eq 1 ]; then
    pass "no $what"
  elif [ "$rc" -eq 0 ]; then
    fail "$what is in the image"
  else
    fail "could not check $what (exit $rc)"
  fi
}

# `command -v` exits 127 when the name is not found, which is also what docker
# exits when an entrypoint is missing; this probe answers 1 for "not found".
# The $1 inside the quotes is the in-container sh's argument, not this one's.
expect_no_command() {
  # shellcheck disable=SC2016
  expect_absent "$1" sh -c 'command -v "$1" > /dev/null || exit 1' sh "$1"
}

# A module that fails to import exits 1 (ModuleNotFoundError).
expect_no_module() {
  local python="$1" module="$2" label="$3"
  expect_absent "$label" "$python" -c "import $module"
}

# <what> <probe...>: the probe must exit 0.
expect_runs() {
  local what="$1" rc
  shift
  rc="$(status_of "$@")"
  if [ "$rc" -eq 0 ]; then
    pass "$what"
  else
    fail "$what failed (exit $rc)"
  fi
}

# <what> <expected> <probe...>: the probe must exit 0 and print <expected>.
expect_output() {
  local what="$1" expected="$2" out rc=0
  shift 2
  out="$(probe "$@" 2> /dev/null)" || rc=$?
  if [ "$rc" -eq 0 ] && [ "$out" = "$expected" ]; then
    pass "$what is $expected"
  else
    fail "$what is '$out' (exit $rc), expected '$expected'"
  fi
}

check_api() {
  local name
  for name in pytest fakeredis playwright; do
    expect_no_module python "$name" "$name (dev extra)"
  done
  for name in ruff pyright gcc cc make uv uvx; do
    expect_no_command "$name"
  done
  # The base image's packaging tools sit outside .venv, in the system Python.
  for name in pip setuptools wheel; do
    expect_no_module /usr/local/bin/python3 "$name" "$name (system Python)"
  done
  expect_output "the user id" 1000 id -u
  expect_runs "the application imports" python -c "import src.api.main"
  expect_runs "alembic runs" alembic --help
  expect_runs "curl (the health check) runs" curl --version
}

check_web() {
  local name major
  for name in npm npx corepack yarn yarnpkg; do
    expect_no_command "$name"
  done
  expect_output "the user" nextjs id -un
  expect_runs "server.js is present" sh -c 'test -f server.js'
  # The base may be pinned by digest: node:24-alpine@sha256:... AS base.
  major="$(sed -nE 's/^FROM node:([0-9]+)-alpine(@sha256:[0-9a-f]{64})? AS base$/\1/p' "$web_dockerfile")"
  if [ -z "$major" ]; then
    fail "no 'FROM node:<major>-alpine AS base' in $web_dockerfile"
  else
    expect_output "Node's major" "$major" node -p 'process.versions.node.split(".")[0]'
  fi
}

# Every absent check below relies on the image starting at all.
rc="$(status_of sh -c 'exit 0')"
if [ "$rc" -ne 0 ]; then
  echo "::error::cannot run $image (exit $rc)"
  exit 2
fi

"check_$kind"

if [ "$failures" -gt 0 ]; then
  echo "$failures check(s) failed for $image"
  exit 1
fi
echo "all checks passed for $image"
