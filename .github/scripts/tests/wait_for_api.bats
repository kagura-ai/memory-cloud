#!/usr/bin/env bats
# Pins the verdicts of .github/scripts/wait-for-api.sh (#1500): ready, process
# died (phase named), a phase cap tripped (phase named), budget exhausted (phase
# named + log tail), and argument validation. The probe is stubbed through the
# CURL env indirection, the same way deploy.sh's bats suites stub curl.

SCRIPT="$BATS_TEST_DIRNAME/../wait-for-api.sh"

mock_curl() {
  [ "${FAKE_CURL_OK:-0}" = "1" ] && return 0
  return 22
}

setup() {
  TMP="$(mktemp -d)"
  export -f mock_curl
  export CURL=mock_curl
  export WAIT_FOR_API_INTERVAL=1
  LOG="$TMP/api.log"
  : > "$LOG"
  sleep 60 &
  LIVE_PID=$!
}

teardown() {
  kill "$LIVE_PID" 2>/dev/null || true
  rm -rf "$TMP"
}

@test "exits 0 as soon as the health endpoint answers" {
  FAKE_CURL_OK=1 run "$SCRIPT" "$LIVE_PID" "$LOG" http://x/health 5
  [ "$status" -eq 0 ]
  [[ "$output" == *"API ready after"* ]]
}

@test "a dead process fails immediately and names the phase from the log" {
  echo "application_starting" >> "$LOG"
  sleep 0.1 &
  dead=$!
  wait "$dead"
  run "$SCRIPT" "$dead" "$LOG" http://x/health 30
  [ "$status" -eq 1 ]
  [[ "$output" == *"exited during phase: lifespan"* ]]
  [[ "$output" == *"::group::last 50 lines"* ]]
  [[ "$output" == *"application_starting"* ]]
}

@test "import cap trips when application_starting never appears" {
  WAIT_FOR_API_STARTING_CAP=1 run "$SCRIPT" "$LIVE_PID" "$LOG" http://x/health 30
  [ "$status" -eq 1 ]
  [[ "$output" == *"still importing after"* ]]
  [[ "$output" == *"application_starting not logged within 1s"* ]]
  [[ "$output" == *"(no log output)"* ]]
}

@test "lifespan cap trips when application_started never appears" {
  echo "application_starting" >> "$LOG"
  WAIT_FOR_API_STARTED_CAP=1 run "$SCRIPT" "$LIVE_PID" "$LOG" http://x/health 30
  [ "$status" -eq 1 ]
  [[ "$output" == *"lifespan not finished after"* ]]
  [[ "$output" == *"application_started not logged within 1s"* ]]
}

@test "budget exhausted names serving once lifespan finished" {
  printf 'application_starting\napplication_started\n' >> "$LOG"
  run "$SCRIPT" "$LIVE_PID" "$LOG" http://x/health 2
  [ "$status" -eq 1 ]
  [[ "$output" == *"not ready after"* ]]
  [[ "$output" == *"stuck in phase: serving"* ]]
}

@test "a non-integer interval is rejected instead of breaking the loop" {
  WAIT_FOR_API_INTERVAL=0.5 run "$SCRIPT" "$LIVE_PID" "$LOG" http://x/health 5
  [ "$status" -eq 2 ]
  [[ "$output" == *"interval must be a positive integer"* ]]
}
