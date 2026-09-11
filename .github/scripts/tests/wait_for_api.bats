#!/usr/bin/env bats
# Pins the three verdicts of .github/scripts/wait-for-api.sh (#1500): ready,
# process died (phase named), budget exhausted (phase named + log tail).
# `curl` is shadowed with a stub on PATH so no network is touched.

SCRIPT="$BATS_TEST_DIRNAME/../wait-for-api.sh"

setup() {
  TMP="$(mktemp -d)"
  mkdir -p "$TMP/bin"
  cat > "$TMP/bin/curl" <<'STUB'
#!/usr/bin/env bash
[ "${FAKE_CURL_OK:-0}" = "1" ] && exit 0
exit 22
STUB
  chmod +x "$TMP/bin/curl"
  export PATH="$TMP/bin:$PATH"
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

@test "budget exhausted names the phase — import when nothing was logged" {
  run "$SCRIPT" "$LIVE_PID" "$LOG" http://x/health 2
  [ "$status" -eq 1 ]
  [[ "$output" == *"not ready after 2s"* ]]
  [[ "$output" == *"stuck in phase: import"* ]]
  [[ "$output" == *"(no log output)"* ]]
}

@test "budget exhausted names serving once lifespan finished" {
  printf 'application_starting\napplication_started\n' >> "$LOG"
  run "$SCRIPT" "$LIVE_PID" "$LOG" http://x/health 2
  [ "$status" -eq 1 ]
  [[ "$output" == *"stuck in phase: serving"* ]]
}
