#!/usr/bin/env bats
# =============================================================================
# Regression suite for deploy.sh `verify_internal_blocked` — the edge-block
# security gate for /internal/* (the #954 billing entitlement-push surface).
# It shares the proven `_verify_path_blocked` loop with verify_workers_blocked
# (see deploy_verify_workers_blocked.bats for the #986 fail-closed details);
# these tests pin that the /internal gate is wired to the same 404 contract and
# stays fail-CLOSED, so a missing `handle /internal*` Caddy block aborts the
# deploy instead of silently shipping an exposed entitlement-write endpoint.
#
# Same harness as the workers suite: the script is sourced and the probe is
# driven through an injected `$CURL` stub fed by a per-attempt sequence file.
# No network, no docker.
# =============================================================================

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
    export SEQ_FILE="$BATS_TEST_TMPDIR/seq"
    export SEQ_IDX="$BATS_TEST_TMPDIR/idx"
    export -f mock_curl

    export CADDYFILE_TPL="$BATS_TEST_TMPDIR/Caddyfile.tpl"
    printf 'memory.example.test {\n    reverse_proxy api:8000\n}\n' > "$CADDYFILE_TPL"

    export CURL=mock_curl
    source "$BATS_TEST_DIRNAME/../deploy.sh"
    set +u

    WORKERS_GATE_TIMEOUT=10
    WORKERS_GATE_INTERVAL=1
}

@test "internal 000 -> 404: first probe connection-refused, retry sees 404 -> returns 0" {
    printf 'FAIL\n404\n' > "$SEQ_FILE"; echo 0 > "$SEQ_IDX"

    run verify_internal_blocked

    [ "$status" -eq 0 ]
    [[ "$output" == *"/internal/*"* ]]
    [[ "$output" == *"correctly blocked (HTTP 404"* ]]
}

@test "internal timeout: exposed API answers a stable 405 (GET on PUT route), never 404 -> aborts loudly, fail-closed" {
    printf '405\n' > "$SEQ_FILE"; echo 0 > "$SEQ_IDX"
    WORKERS_GATE_TIMEOUT=1

    run verify_internal_blocked

    [ "$status" -ne 0 ]
    [[ "$output" == *"/internal/*"* ]]
    [[ "$output" == *"NOT blocked"* ]]
    [[ "$output" == *"expected 404"* ]]
    [[ "$output" != *"correctly blocked"* ]]
}

@test "internal set -e safety: a connection-refused probe does NOT silently kill the caller (#986 guard)" {
    printf 'FAIL\n404\n' > "$SEQ_FILE"; echo 0 > "$SEQ_IDX"

    run bash -c '
        set -euo pipefail
        source "'"$BATS_TEST_DIRNAME"'/../deploy.sh"
        WORKERS_GATE_TIMEOUT=10 WORKERS_GATE_INTERVAL=1
        verify_internal_blocked >/dev/null 2>&1
        echo "STEP7_REACHED"
    '

    [ "$status" -eq 0 ]
    [[ "$output" == *"STEP7_REACHED"* ]]
}
