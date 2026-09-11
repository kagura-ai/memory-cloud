#!/usr/bin/env bash
# Phase-aware readiness wait for an API started in a previous CI step (#1500).
#
# The old gate was `curl /health` for a fixed 60s: when the API was merely slow
# to boot, the job went red in the a11y step with a locator timeout, and the
# log never said what the API was still doing. This script waits on the
# health endpoint while watching the process and its log, and on failure
# names the boot phase it was blocked in and prints the log tail.
#
# Phases are read from the app's own structlog lines (LOG_LEVEL=INFO):
#   import   — python process alive, `application_starting` not logged yet
#   lifespan — between `application_starting` and `application_started`
#   serving  — lifespan finished, health endpoint not answering yet
#
# Usage: wait-for-api.sh <pid> <log-file> [health-url] [budget-seconds]
set -euo pipefail

pid="${1:?usage: wait-for-api.sh <pid> <log-file> [health-url] [budget-seconds]}"
log="${2:?usage: wait-for-api.sh <pid> <log-file> [health-url] [budget-seconds]}"
url="${3:-http://localhost:8080/health}"
budget="${4:-180}"
interval="${WAIT_FOR_API_INTERVAL:-2}"

phase_of_log() {
  if grep -q "application_started" "$log" 2>/dev/null; then
    echo "serving (lifespan finished; ${url} not answering)"
  elif grep -q "application_starting" "$log" 2>/dev/null; then
    echo "lifespan (auth managers, scheduler, backfill)"
  else
    echo "import (python process alive, app module not loaded yet)"
  fi
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

elapsed=0
while [ "$elapsed" -lt "$budget" ]; do
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "::error::API process ${pid} exited during phase: $(phase_of_log) (after ${elapsed}s)"
    dump_log
    exit 1
  fi
  if curl -sf --max-time 2 "$url" >/dev/null 2>&1; then
    echo "API ready after ${elapsed}s"
    exit 0
  fi
  sleep "$interval"
  elapsed=$((elapsed + interval))
done

echo "::error::API not ready after ${budget}s — stuck in phase: $(phase_of_log)"
dump_log
exit 1
