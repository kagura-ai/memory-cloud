#!/usr/bin/env bash
# Phase-aware readiness wait for an API started in a previous CI step (#1500).
#
# Waits on the health endpoint while watching the process and its log. On
# failure it names the boot phase the API was blocked in and prints the log
# tail, so a slow or crashed boot reads as an environment failure — never as a
# locator timeout in the test step that follows.
#
# Phases are read from the app's own structlog lines (LOG_LEVEL=INFO). The two
# event names are pinned by backend/tests/test_api_boot_phase_markers.py:
#   import   — python process alive, `application_starting` not logged yet
#   lifespan — between `application_starting` and `application_started`
#   serving  — lifespan finished, health endpoint not answering yet
#
# Each phase has its own cap so a hung boot is reported at the phase that hung
# instead of after the whole budget: import must end within
# WAIT_FOR_API_STARTING_CAP seconds, lifespan within WAIT_FOR_API_STARTED_CAP,
# and only the serving phase may use the full budget. Time is measured with
# bash's SECONDS, so the probe's own --max-time counts against the budget.
#
# Usage: wait-for-api.sh <pid> <log-file> [health-url] [budget-seconds]
# Env:   CURL (probe command, default curl — bats stubs it the deploy.sh way),
#        WAIT_FOR_API_INTERVAL (poll seconds, integer, default 2),
#        WAIT_FOR_API_STARTING_CAP (default 45), WAIT_FOR_API_STARTED_CAP (120).
set -euo pipefail

pid="${1:?usage: wait-for-api.sh <pid> <log-file> [health-url] [budget-seconds]}"
log="${2:?usage: wait-for-api.sh <pid> <log-file> [health-url] [budget-seconds]}"
url="${3:-http://localhost:8080/health}"
budget="${4:-180}"
interval="${WAIT_FOR_API_INTERVAL:-2}"
starting_cap="${WAIT_FOR_API_STARTING_CAP:-45}"
started_cap="${WAIT_FOR_API_STARTED_CAP:-120}"
CURL="${CURL:-curl}"

for name in budget interval starting_cap started_cap; do
  if ! [[ "${!name}" =~ ^[1-9][0-9]*$ ]]; then
    echo "::error::wait-for-api.sh: ${name} must be a positive integer (got '${!name}')"
    exit 2
  fi
done

phase() {
  if grep -q "application_started" "$log" 2>/dev/null; then
    echo "serving"
  elif grep -q "application_starting" "$log" 2>/dev/null; then
    echo "lifespan"
  else
    echo "import"
  fi
}

describe() {
  case "$1" in
    serving) echo "serving (lifespan finished; ${url} not answering)" ;;
    lifespan) echo "lifespan (auth managers, scheduler, backfill)" ;;
    *) echo "import (python process alive, app module not loaded yet)" ;;
  esac
}

dump_log() {
  echo "::group::last 50 lines of ${log}"
  if [ -s "$log" ]; then
    tail -n 50 "$log"
  else
    echo "(no log output)"
  fi
  echo "::endgroup::"
}

fail() {
  echo "::error::$1"
  dump_log
  exit 1
}

SECONDS=0
while [ "$SECONDS" -lt "$budget" ]; do
  if ! kill -0 "$pid" 2>/dev/null; then
    fail "API process ${pid} exited during phase: $(describe "$(phase)") (after ${SECONDS}s)"
  fi
  if "$CURL" -sf --max-time 2 "$url" >/dev/null 2>&1; then
    echo "API ready after ${SECONDS}s"
    exit 0
  fi
  current="$(phase)"
  if [ "$current" = "import" ] && [ "$SECONDS" -ge "$starting_cap" ]; then
    fail "API still importing after ${SECONDS}s (application_starting not logged within ${starting_cap}s)"
  fi
  if [ "$current" = "lifespan" ] && [ "$SECONDS" -ge "$started_cap" ]; then
    fail "API lifespan not finished after ${SECONDS}s (application_started not logged within ${started_cap}s)"
  fi
  sleep "$interval"
done

fail "API not ready after ${SECONDS}s — stuck in phase: $(describe "$(phase)")"
