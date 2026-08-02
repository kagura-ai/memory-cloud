#!/usr/bin/env bats
# =============================================================================
# #1476: after a blue-green color flip, deploy.sh must restart the co-resident
# chat-bridge worker.
#
# The worker resolves the API container by color when it connects and then holds
# those connections. Without this restart it keeps talking to the color being
# drained and survives only through kagura-bridge's connect-level cross-color
# fallback — which absorbed 4162 calls over 65 hours in the 2026-07-21..24
# incident, and let Slack ingest sit dead for ~30 hours on 2026-07-29 before
# anyone noticed. The step was missing across v0.61.0, v0.62.0 and v0.63.0, each
# needing the operator to remember a manual `docker restart`.
#
# The three properties that make it safe to run unconditionally, and which this
# file pins:
#   1. present  -> restarts it
#   2. absent   -> logs a skip and succeeds (deploy.sh is also the OSS script)
#   3. failing  -> logs a warning and does NOT abort a completed cutover
#
# deploy.sh guards `main` behind BASH_SOURCE, so these call the real functions
# through an injected `$DOCKER` stub. No docker, no network.
# =============================================================================

DEPLOY_SH="$BATS_TEST_DIRNAME/../deploy.sh"

# Stub for the docker indirection. Driven by files so the third test's fresh
# `bash -c` child (which re-sources the script) can share the same state:
#   $PS_FILE      — what `docker ps --format '{{.Names}}'` prints
#   $RESTART_LOG  — appended with the container name on every `docker restart`
#   $RESTART_RC   — exit code `docker restart` returns (default 0)
mock_docker() {
    case "${1:-}" in
        ps)
            cat "$PS_FILE"
            ;;
        restart)
            echo "$2" >> "$RESTART_LOG"
            return "${RESTART_RC:-0}"
            ;;
        *)
            return 0
            ;;
    esac
}

setup() {
    [ -r "$DEPLOY_SH" ] || return 1

    # `mktemp -d` rather than $BATS_TEST_TMPDIR: that variable only exists from
    # bats-core 1.4, and the distro bats on older runners (1.2.1 on Ubuntu
    # 22.04) leaves it unset — under which every path below collapses to `/ps`
    # and the whole file fails in setup(). deploy_marker_integrity.bats already
    # uses mktemp for the same reason.
    TMP="$(mktemp -d)"
    export PS_FILE="$TMP/ps"
    export RESTART_LOG="$TMP/restarts"
    export RESTART_RC=0
    : > "$PS_FILE"
    : > "$RESTART_LOG"

    # Exported so a child `bash -c` that re-sources deploy.sh keeps the stub
    # (the script's own `DOCKER="${DOCKER:-docker}"` preserves it).
    export DOCKER=mock_docker
    export -f mock_docker

    # shellcheck disable=SC1090
    source "$DEPLOY_SH"

    # Neutralize the EXIT trap deploy.sh installs — a stray final-status line
    # would pollute $output.
    trap - EXIT

    # deploy.sh sets `-euo pipefail` and sourcing leaves those on.
    #
    # `-e` must STAY ON: bats detects a failing test through it, so `set +e`
    # would make every assertion advisory (a body ending in `false || return 1`
    # would still report `ok`). Only `-u` is dropped, because an unbound-variable
    # death inside a test aborts the whole file instead of failing one case.
    set +u
}

teardown() {
    [ -n "${TMP:-}" ] && rm -rf "$TMP"
}

# --- 1. present -> restarts -------------------------------------------------

@test "the bridge worker is restarted when it is running" {
    printf 'kagura-api-blue\nkagura-bridge-worker-1\nkagura-web\n' > "$PS_FILE"

    run restart_bridge_worker

    [ "$status" -eq 0 ] || return 1
    [[ "$output" == *"Restarting kagura-bridge-worker-1"* ]] || return 1
    [[ "$output" == *"restarted."* ]] || return 1
    [ "$(cat "$RESTART_LOG")" = "kagura-bridge-worker-1" ] || return 1
}

@test "BRIDGE_WORKER_CONTAINER overrides which container is restarted" {
    BRIDGE_WORKER_CONTAINER="bridge-staging"
    printf 'bridge-staging\n' > "$PS_FILE"

    run restart_bridge_worker

    [ "$status" -eq 0 ] || return 1
    [ "$(cat "$RESTART_LOG")" = "bridge-staging" ] || return 1
}

# --- 2. absent -> skip, exit 0 ---------------------------------------------

@test "a host with no bridge container logs a skip and succeeds" {
    printf 'kagura-api-blue\nkagura-web\nkagura-postgres\n' > "$PS_FILE"

    run restart_bridge_worker

    [ "$status" -eq 0 ] || return 1
    [[ "$output" == *"not running on this host"* ]] || return 1
    # And it must not have tried anyway.
    [ ! -s "$RESTART_LOG" ] || return 1
}

@test "an empty docker ps is a skip, not a crash" {
    : > "$PS_FILE"

    run restart_bridge_worker

    [ "$status" -eq 0 ] || return 1
    [[ "$output" == *"not running on this host"* ]] || return 1
    [ ! -s "$RESTART_LOG" ] || return 1
}

@test "a container whose name merely CONTAINS the target does not satisfy the check" {
    # `kagura-bridge-worker-10` and a differently-prefixed worker both share a
    # substring with `kagura-bridge-worker-1`. A `grep` without whole-line
    # anchoring would restart the wrong container — or claim the right one is
    # present when it is not.
    printf 'kagura-bridge-worker-10\nold-kagura-bridge-worker-1\n' > "$PS_FILE"

    run restart_bridge_worker

    [ "$status" -eq 0 ] || return 1
    [[ "$output" == *"not running on this host"* ]] || return 1
    [ ! -s "$RESTART_LOG" ] || return 1
}

@test "presence detection does not depend on a pipeline status (the #986 shape)" {
    # `docker ps | grep -q` can SIGPIPE the producer once grep exits early, and
    # under `set -o pipefail` that reports a RUNNING container as absent —
    # intermittently. Pin the property directly: a long name list with the
    # target on the first line still detects it.
    {
        echo "kagura-bridge-worker-1"
        for i in $(seq 1 500); do echo "filler-container-$i"; done
    } > "$PS_FILE"

    run bridge_worker_is_running

    [ "$status" -eq 0 ] || return 1
}

# --- 3. failing restart -> warn, do not abort -------------------------------

@test "a failing restart logs a warning and still returns 0" {
    printf 'kagura-bridge-worker-1\n' > "$PS_FILE"
    export RESTART_RC=1

    run restart_bridge_worker

    [ "$status" -eq 0 ] || return 1
    [[ "$output" == *"WARNING"* ]] || return 1
    [[ "$output" == *"failed to restart"* ]] || return 1
    # The operator needs the recovery command, not just the complaint.
    [[ "$output" == *"docker restart kagura-bridge-worker-1"* ]] || return 1
    # It must not claim success.
    [[ "$output" != *"kagura-bridge-worker-1 restarted."* ]] || return 1
}

@test "set -e safety: a failing restart does NOT kill the caller mid-deploy" {
    # The step sits between the Caddy switch and the drain. If a non-zero
    # `docker restart` propagated, `set -euo pipefail` would kill the run right
    # there and Step 7 would never drain the old color — the API cutover would
    # be complete but the deploy would report ABORTED. bats neutralises errexit
    # inside its own test shell, so this is only observable in a child process
    # bats does not manage.
    printf 'kagura-bridge-worker-1\n' > "$PS_FILE"
    export RESTART_RC=1

    run bash -c '
        set -euo pipefail
        source "'"$DEPLOY_SH"'"
        trap - EXIT
        restart_bridge_worker >/dev/null 2>&1
        echo "STEP7_REACHED"
    '

    [ "$status" -eq 0 ] || return 1
    [[ "$output" == *"STEP7_REACHED"* ]] || return 1
}

@test "set -e safety: a docker ps that fails outright is a skip, not a death" {
    # `docker ps` returning non-zero (daemon unreachable, permission denied)
    # must not take the deploy with it.
    run bash -c '
        set -euo pipefail
        broken_docker() { return 1; }
        export -f broken_docker
        DOCKER=broken_docker
        source "'"$DEPLOY_SH"'"
        trap - EXIT
        restart_bridge_worker >/dev/null 2>&1
        echo "SURVIVED"
    '

    [ "$status" -eq 0 ] || return 1
    [[ "$output" == *"SURVIVED"* ]] || return 1
}

# --- Call sites -------------------------------------------------------------
# Static checks: calling cmd_deploy/cmd_rollback for real would build images and
# start containers. What matters is that the call exists and sits AFTER the
# Caddy switch — restarting before it would re-pin the worker to the old color,
# which is worse than not restarting at all, and no runtime assertion here could
# tell the two orders apart.

@test "cmd_deploy restarts the bridge worker" {
    run bash -o pipefail -c 'sed -n "/^cmd_deploy()/,/^}/p" "$1" | grep -c "restart_bridge_worker"' _ "$DEPLOY_SH"
    [ "$status" -eq 0 ] || return 1
    [ "$output" -ge 1 ] || return 1
}

@test "cmd_deploy restarts it AFTER the Caddy switch, not before" {
    run bash -o pipefail -c '
        body=$(sed -n "/^cmd_deploy()/,/^}/p" "$1")
        switch=$(printf "%s\n" "$body" | grep -n "^ *reload_caddy" | head -1 | cut -d: -f1)
        restart=$(printf "%s\n" "$body" | grep -n "^ *restart_bridge_worker" | head -1 | cut -d: -f1)
        [ -n "$switch" ] && [ -n "$restart" ] && [ "$restart" -gt "$switch" ]
    ' _ "$DEPLOY_SH"
    [ "$status" -eq 0 ] || return 1
}

@test "rollback flips the color too, so it restarts the bridge worker as well" {
    run bash -o pipefail -c 'sed -n "/^cmd_rollback()/,/^}/p" "$1" | grep -c "restart_bridge_worker"' _ "$DEPLOY_SH"
    [ "$status" -eq 0 ] || return 1
    [ "$output" -ge 1 ] || return 1
}

@test "cmd_rollback restarts it AFTER the Caddy switch, not before" {
    run bash -o pipefail -c '
        body=$(sed -n "/^cmd_rollback()/,/^}/p" "$1")
        switch=$(printf "%s\n" "$body" | grep -n "^ *reload_caddy" | head -1 | cut -d: -f1)
        restart=$(printf "%s\n" "$body" | grep -n "^ *restart_bridge_worker" | head -1 | cut -d: -f1)
        [ -n "$switch" ] && [ -n "$restart" ] && [ "$restart" -gt "$switch" ]
    ' _ "$DEPLOY_SH"
    [ "$status" -eq 0 ] || return 1
}

@test "--web does not touch the bridge worker (it never changes the API color)" {
    run bash -o pipefail -c 'sed -n "/^cmd_deploy_web()/,/^}/p" "$1" | grep -c "restart_bridge_worker"' _ "$DEPLOY_SH"
    # grep -c prints 0 and exits 1 when there are no matches.
    [ "$output" = "0" ] || return 1
}
