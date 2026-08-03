#!/usr/bin/env bats
# =============================================================================
# #1480: the deploy must PROVE the bridge worker landed on the new color.
#
# #1477 added Step 6b and reported "kagura-bridge-worker-1 restarted." — the
# action, not the outcome. On the real 2026-08-03 deploy that line printed while
# the worker was still pinned to the drained color, and the run finished
# "FINAL STATUS: SUCCESS". The worker's own /health returns {"status":"ok"}
# throughout, so nothing else could catch it.
#
# So: a restart that does not re-point is a DEGRADED run, and must read as one.
# It is still not an abort — the API cutover has already succeeded by then, and
# failing a healthy cutover invites the needless rollback that got the manual
# step skipped in the first place.
# =============================================================================

DEPLOY_SH="$BATS_TEST_DIRNAME/../deploy.sh"

# $DOCKER stub. Driven by files so a child `bash -c` inherits the state.
#   $PS_FILE      — `docker ps --format '{{.Names}}'` output
#   $ENV_FILE_OUT — `docker inspect --format '{{range .Config.Env}}...'` output
#   $LOGS_FILE    — `docker logs` output
#   $INSPECT_RC   — exit code for inspect (default 0)
mock_docker() {
    case "${1:-}" in
        ps) cat "$PS_FILE" ;;
        inspect)
            # NB: `return $RC && cat ...` would exit before the cat ever ran,
            # making every pin look empty and silently passing the
            # unpinned-path tests. Branch explicitly.
            if [ "${INSPECT_RC:-0}" -ne 0 ]; then
                return "${INSPECT_RC}"
            fi
            cat "$ENV_FILE_OUT"
            ;;
        logs)    cat "$LOGS_FILE" ;;
        restart) return "${RESTART_RC:-0}" ;;
        *)       return 0 ;;
    esac
}

setup() {
    [ -r "$DEPLOY_SH" ] || return 1
    TMP="$(mktemp -d)"
    export PS_FILE="$TMP/ps" ENV_FILE_OUT="$TMP/env" LOGS_FILE="$TMP/logs"
    export INSPECT_RC=0 RESTART_RC=0
    printf 'kagura-bridge-worker-1\n' > "$PS_FILE"
    : > "$ENV_FILE_OUT"
    : > "$LOGS_FILE"

    export DOCKER=mock_docker
    export -f mock_docker

    # shellcheck disable=SC1090
    source "$DEPLOY_SH"
    trap - EXIT
    set +u
    BRIDGE_STEP_DEGRADED=0
}

teardown() {
    [ -n "${TMP:-}" ] && rm -rf "$TMP"
}

# --- reading the pin --------------------------------------------------------

@test "a pin naming the new color verifies clean" {
    printf 'PATH=/usr/bin\nKMC_INTERNAL_URL=http://api-green:8080\nLOG_LEVEL=INFO\n' > "$ENV_FILE_OUT"
    run verify_bridge_upstream green
    [ "$status" -eq 0 ] || return 1
    [[ "$output" == *"matches api-green"* ]] || return 1
}

@test "a pin naming the DRAINED color is caught (the exact 2026-08-03 state)" {
    printf 'KMC_INTERNAL_URL=http://api-blue:8080\n' > "$ENV_FILE_OUT"
    run verify_bridge_upstream green
    [ "$status" -ne 0 ] || return 1
    [[ "$output" == *"WARNING"* ]] || return 1
    [[ "$output" == *"NOT api-green"* ]] || return 1
}

@test "the warning says a restart cannot fix it, and gives the recreate command" {
    # The operator's instinct is to restart again. That is precisely what does
    # not work, and the message has to say so or the loop repeats.
    printf 'KMC_INTERNAL_URL=http://api-blue:8080\n' > "$ENV_FILE_OUT"
    run verify_bridge_upstream green
    [[ "$output" == *"restart CANNOT fix this"* ]] || return 1
    [[ "$output" == *"force-recreate"* ]] || return 1
}

@test "no static pin means the worker follows the marker, which is fine" {
    printf 'PATH=/usr/bin\nKMC_INTERNAL_URL_TEMPLATE=http://api-{color}:8080\n' > "$ENV_FILE_OUT"
    run verify_bridge_upstream green
    [ "$status" -eq 0 ] || return 1
    [[ "$output" == *"follows the marker"* ]] || return 1
}

@test "an empty pin is treated as unpinned, not as a mismatch" {
    printf 'KMC_INTERNAL_URL=\n' > "$ENV_FILE_OUT"
    run verify_bridge_upstream green
    [ "$status" -eq 0 ] || return 1
    [[ "$output" == *"follows the marker"* ]] || return 1
}

@test "a pin for api-green must not satisfy a check for api-blue" {
    # Guards against a substring/always-true comparison.
    printf 'KMC_INTERNAL_URL=http://api-green:8080\n' > "$ENV_FILE_OUT"
    run verify_bridge_upstream blue
    [ "$status" -ne 0 ] || return 1
}

@test "an unreadable docker inspect is 'not verified', never 'verified'" {
    export INSPECT_RC=1
    run verify_bridge_upstream green
    [ "$status" -ne 0 ] || return 1
    [[ "$output" == *"NOT verified"* ]] || return 1
}

# --- the worker's own verdict ----------------------------------------------

@test "cross-color fallback in the logs fails verification even with a correct pin" {
    # A pin can look right while the worker is still stranded. Its own log line
    # is the ground truth, and it is the ONLY outward sign — /health stays ok.
    printf 'KMC_INTERNAL_URL=http://api-green:8080\n' > "$ENV_FILE_OUT"
    printf '%s\n' '{"event":"control_plane.cross_color_fallback","reason":"ConnectError"}' > "$LOGS_FILE"
    run verify_bridge_upstream green
    [ "$status" -ne 0 ] || return 1
    [[ "$output" == *"cross-color fallback"* ]] || return 1
}

@test "clean logs with a correct pin verify" {
    printf 'KMC_INTERNAL_URL=http://api-green:8080\n' > "$ENV_FILE_OUT"
    printf '%s\n' '{"event":"multi_tenant_supervisor.start"}' > "$LOGS_FILE"
    run verify_bridge_upstream green
    [ "$status" -eq 0 ] || return 1
}

# --- degraded reporting -----------------------------------------------------

@test "a mismatch marks the run degraded without failing it" {
    printf 'KMC_INTERNAL_URL=http://api-blue:8080\n' > "$ENV_FILE_OUT"
    local rc=0
    restart_bridge_worker green || rc=$?
    # The API cutover already succeeded; the step must not abort...
    [ "$rc" -eq 0 ] || return 1
    # ...but it must not read as clean either.
    [ "$BRIDGE_STEP_DEGRADED" -eq 1 ] || return 1
}

@test "a verified worker leaves the run clean" {
    printf 'KMC_INTERNAL_URL=http://api-green:8080\n' > "$ENV_FILE_OUT"
    restart_bridge_worker green
    [ "$BRIDGE_STEP_DEGRADED" -eq 0 ] || return 1
}

@test "report_bridge_state is silent when clean and unmissable when not" {
    BRIDGE_STEP_DEGRADED=0
    run report_bridge_state
    [ -z "$output" ] || return 1

    BRIDGE_STEP_DEGRADED=1
    run report_bridge_state
    [[ "$output" == *"BRIDGE NOT VERIFIED"* ]] || return 1
    [[ "$output" == *"--verify-bridge"* ]] || return 1
}

@test "a failed restart also marks the run degraded" {
    export RESTART_RC=1
    printf 'KMC_INTERNAL_URL=http://api-green:8080\n' > "$ENV_FILE_OUT"
    restart_bridge_worker green
    [ "$BRIDGE_STEP_DEGRADED" -eq 1 ] || return 1
}

@test "an absent bridge is not degraded — the OSS single-server case" {
    printf 'kagura-api-blue\n' > "$PS_FILE"
    restart_bridge_worker green
    [ "$BRIDGE_STEP_DEGRADED" -eq 0 ] || return 1
}

# --- wiring -----------------------------------------------------------------

@test "both flip paths pass the NEW color to restart_bridge_worker" {
    # Passing no argument would compare against an empty string and 'verify'
    # nothing — the failure mode this whole file exists to prevent.
    run bash -o pipefail -c 'sed -n "/^cmd_deploy()/,/^}/p" "$1" | grep -cE "^ *restart_bridge_worker \"\\\$inactive\""' _ "$DEPLOY_SH"
    [ "$output" -ge 1 ] || return 1
    run bash -o pipefail -c 'sed -n "/^cmd_rollback()/,/^}/p" "$1" | grep -cE "^ *restart_bridge_worker \"\\\$inactive\""' _ "$DEPLOY_SH"
    [ "$output" -ge 1 ] || return 1
}

@test "both flip paths report the bridge verdict in their summary" {
    run bash -o pipefail -c 'sed -n "/^cmd_deploy()/,/^}/p" "$1" | grep -cE "^ *report_bridge_state"' _ "$DEPLOY_SH"
    [ "$output" -ge 1 ] || return 1
    run bash -o pipefail -c 'sed -n "/^cmd_rollback()/,/^}/p" "$1" | grep -cE "^ *report_bridge_state"' _ "$DEPLOY_SH"
    [ "$output" -ge 1 ] || return 1
}

@test "--verify-bridge is a real dispatch target, not just help text" {
    run bash -o pipefail -c 'grep -c -- "--verify-bridge)" "$1"' _ "$DEPLOY_SH"
    [ "$output" -ge 1 ] || return 1
}
