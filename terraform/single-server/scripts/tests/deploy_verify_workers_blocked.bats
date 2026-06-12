#!/usr/bin/env bats
# =============================================================================
# Regression suite for deploy.sh `verify_workers_blocked` — the #986 security
# gate. The bare `http_status=$(curl ...)` assignment died silently under
# `set -euo pipefail` on a connection-refused probe (curl exit 7), skipping
# Step 7 (drain old color). These tests pin all three branches so the bug
# cannot return. See issue #1000 (follow-up to #986).
#
# The script is sourced (it guards its entrypoint with a BASH_SOURCE check),
# and the security-gate probe is driven through an injected `$CURL` stub whose
# per-attempt responses come from a sequence file. No network, no docker.
# =============================================================================

# Stub for the curl indirection in verify_workers_blocked. Behaviour is driven
# by $SEQ_FILE (one token per probe attempt) and a $SEQ_IDX counter file:
#   FAIL  -> exit 7 with NO stdout (connection refused, the #986 trigger)
#   NNN   -> print the HTTP status code NNN on stdout (exit 0)
# Tokens past end-of-file repeat the last line (a "stable" response).
mock_curl() {
    local n line
    n=$(( $(cat "$SEQ_IDX") + 1 ))
    echo "$n" > "$SEQ_IDX"
    line=$(sed -n "${n}p" "$SEQ_FILE")
    if [ -z "$line" ]; then line=$(tail -n 1 "$SEQ_FILE"); fi
    if [ "$line" = "FAIL" ]; then
        return 7
    fi
    printf '%s' "$line"
}

setup() {
    # Exported so the third test's fresh `bash -c` child (which re-sources the
    # script) inherits the stub and its sequence state via the environment.
    export SEQ_FILE="$BATS_TEST_TMPDIR/seq"
    export SEQ_IDX="$BATS_TEST_TMPDIR/idx"
    export -f mock_curl

    # Minimal Caddyfile.tpl fixture so verify_workers_blocked's awk domain
    # extraction has a real file (the domain value is irrelevant — curl is
    # stubbed — but the extraction must not fail under `set -e`).
    export CADDYFILE_TPL="$BATS_TEST_TMPDIR/Caddyfile.tpl"
    printf 'memory.example.test {\n    reverse_proxy api:8000\n}\n' > "$CADDYFILE_TPL"

    # Source the script under test. Drop ONLY nounset afterward: the sourced
    # `set -u` would trip bats' own internal unset-var references, but we must
    # KEEP errexit on — bats relies on it to fail a test when an assertion
    # (`[ ... ]`) returns non-zero. Turning errexit off here would silently make
    # every assertion a no-op (a false-green). `run`/`run bash -c` capture
    # status without tripping errexit, so the assertions stay meaningful.
    # Exported so the script's own `CURL="${CURL:-curl}"` keeps our stub when
    # the third test re-sources it in a child process.
    export CURL=mock_curl
    source "$BATS_TEST_DIRNAME/../deploy.sh"
    set +u

    # Generous, deterministic gate window. The window is wall-clock (`SECONDS`,
    # integer) so it must comfortably fit two probes plus one interval despite
    # subprocess-spawn overhead — TIMEOUT=10/INTERVAL=1 gives ~10 retries of
    # headroom while the cases only need two.
    WORKERS_GATE_TIMEOUT=10
    WORKERS_GATE_INTERVAL=1
}

@test "000 -> 404: first probe connection-refused, retry sees 404 -> returns 0 (the #986 race)" {
    printf 'FAIL\n404\n' > "$SEQ_FILE"; echo 0 > "$SEQ_IDX"

    run verify_workers_blocked

    [ "$status" -eq 0 ]
    [[ "$output" == *"correctly blocked (HTTP 404"* ]]
}

@test "timeout: stable 200, never 404 -> aborts loudly, fail-closed (old color NOT drained)" {
    printf '200\n' > "$SEQ_FILE"; echo 0 > "$SEQ_IDX"
    WORKERS_GATE_TIMEOUT=1   # one probe, then deadline — keeps the test ~1s

    run verify_workers_blocked

    [ "$status" -ne 0 ]
    [[ "$output" == *"NOT blocked"* ]]
    [[ "$output" == *"expected 404"* ]]
    [[ "$output" != *"correctly blocked"* ]]
}

@test "set -e safety: a connection-refused probe does NOT silently kill the caller (regression guard for #986)" {
    printf 'FAIL\n404\n' > "$SEQ_FILE"; echo 0 > "$SEQ_IDX"

    # Reproduce cmd_deploy's call site in a FRESH bash under real
    # `set -euo pipefail`. bats neutralises errexit inside its own test shell
    # (even inside a command-substitution subshell), so the #986 silent death
    # can only be observed faithfully in a child process bats does not manage.
    # Before the fix, the curl exit 7 killed this child before "STEP7_REACHED".
    run bash -c '
        set -euo pipefail
        source "'"$BATS_TEST_DIRNAME"'/../deploy.sh"
        WORKERS_GATE_TIMEOUT=10 WORKERS_GATE_INTERVAL=1
        verify_workers_blocked >/dev/null 2>&1
        echo "STEP7_REACHED"
    '

    [ "$status" -eq 0 ]
    [[ "$output" == *"STEP7_REACHED"* ]]
}
