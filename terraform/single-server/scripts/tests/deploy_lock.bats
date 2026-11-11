#!/usr/bin/env bats
# =============================================================================
# #2776: one mutating deploy.sh run at a time.
#
# Two deploys started 23 s apart both reached Step 2 (alembic upgrade head) in
# the same second; the second lost a CREATE TABLE race and stopped by luck.
# Every mutating mode (deploy, --rollback, --web, --generate-caddyfile) now
# takes an exclusive, non-blocking flock on DEPLOY_LOCK_FILE before it does
# anything; a second run exits at once, names the holder and changes nothing.
# --status / --verify-bridge / --help take no lock.
#
# The lock is held from the test itself (flock on its own fd), so these run
# without docker and without a second deploy.
# =============================================================================

DEPLOY_SH="$BATS_TEST_DIRNAME/../deploy.sh"

setup() {
    [ -r "$DEPLOY_SH" ] || return 1
    command -v flock > /dev/null || skip "flock not installed"
    TMP="$(mktemp -d)"
    export DEPLOY_LOCK_FILE="$TMP/lock/deploy.lock"
    mkdir -p "$TMP/lock"
    echo "blue" > "$TMP/active-color"
    echo "caddy-original" > "$TMP/Caddyfile"
}

teardown() {
    release_lock
    [ -n "${TMP:-}" ] && rm -rf "$TMP"
}

# Hold the lock the way another deploy.sh run would: its own open file
# description, flock -n, and a holder line in the file.
hold_lock() {
    exec 8<> "$DEPLOY_LOCK_FILE"
    flock -n 8
    echo "pid=4242 started=2026-11-11T00:00:00Z mode=deploy" > "$DEPLOY_LOCK_FILE"
}

release_lock() {
    exec 8>&- 2> /dev/null || true
}

# Source deploy.sh (main stays guarded by BASH_SOURCE), point every path at
# $TMP, replace each command with a sentinel writer that would also touch the
# marker and Caddyfile, then call main with the given mode. Anything that runs
# past the lock leaves a trace in $TMP.
run_main() {
    run timeout 10 bash -c '
        source "$1"; shift
        tmp="$1"; shift
        MARKER_FILE="$tmp/active-color"
        CADDYFILE="$tmp/Caddyfile"
        COMPOSE_FILE="$tmp/compose.yml"; touch "$COMPOSE_FILE"
        ENV_FILE="$tmp/.env.prod"; touch "$ENV_FILE"
        validate_log_rotation_env() { :; }
        mutate() { echo "$1" > "$tmp/sentinel"; echo green > "$MARKER_FILE"; echo changed > "$CADDYFILE"; }
        cmd_deploy() { mutate deploy; }
        cmd_rollback() { mutate rollback; }
        cmd_deploy_web() { mutate web; }
        cmd_generate_caddyfile() { mutate generate-caddyfile; }
        cmd_status() { echo "STATUS-RAN"; }
        cmd_verify_bridge() { echo "VERIFY-RAN"; }
        main "$@"
    ' _ "$DEPLOY_SH" "$TMP" "$@"
}

assert_untouched() {
    [ ! -e "$TMP/sentinel" ] || return 1
    [ "$(cat "$TMP/active-color")" = "blue" ] || return 1
    [ "$(cat "$TMP/Caddyfile")" = "caddy-original" ] || return 1
    # The holder's line must survive the refused run (no truncating open).
    grep -q "pid=4242" "$DEPLOY_LOCK_FILE" || return 1
}

@test "a deploy while the lock is held exits non-zero, names the holder, changes nothing" {
    hold_lock
    run_main
    [ "$status" -ne 0 ] || return 1
    [ "$status" -ne 124 ] || return 1   # refused at once, did not wait
    [[ "$output" == *"another deploy.sh run holds"* ]] || return 1
    [[ "$output" == *"pid=4242 started=2026-11-11T00:00:00Z"* ]] || return 1
    [[ "$output" == *"$DEPLOY_LOCK_FILE"* ]] || return 1
    assert_untouched
}

@test "--rollback, --web and --generate-caddyfile are refused too" {
    hold_lock
    local mode
    for mode in --rollback --web --generate-caddyfile; do
        run_main "$mode"
        [ "$status" -ne 0 ] || { echo "$mode was not refused"; return 1; }
        [[ "$output" == *"another deploy.sh run holds"* ]] || { echo "$mode: $output"; return 1; }
        assert_untouched || { echo "$mode changed something"; return 1; }
    done
}

@test "the executed script refuses before any prerequisite check or side effect" {
    # Run deploy.sh as a process (not sourced): it must stop at the lock, before
    # it looks for .env.prod or creates the marker directory.
    hold_lock
    run timeout 10 "$DEPLOY_SH"
    [ "$status" -ne 0 ] || return 1
    [ "$status" -ne 124 ] || return 1
    [[ "$output" == *"another deploy.sh run holds"* ]] || return 1
    [[ "$output" != *".env.prod not found"* ]] || return 1
    grep -q "pid=4242" "$DEPLOY_LOCK_FILE" || return 1
}

@test "--status, --verify-bridge and --help run while the lock is held" {
    hold_lock
    run_main --status
    [ "$status" -eq 0 ] || { echo "$output"; return 1; }
    [[ "$output" == *"STATUS-RAN"* ]] || return 1
    run_main --verify-bridge
    [ "$status" -eq 0 ] || { echo "$output"; return 1; }
    [[ "$output" == *"VERIFY-RAN"* ]] || return 1
    run_main --help
    [ "$status" -eq 0 ] || return 1
    assert_untouched
}

@test "after the holder releases, a run takes the lock and records itself" {
    hold_lock
    release_lock
    run_main
    [ "$status" -eq 0 ] || { echo "$output"; return 1; }
    [ "$(cat "$TMP/sentinel")" = "deploy" ] || return 1
    # The new holder wrote its own pid/start over the old line.
    ! grep -q "pid=4242" "$DEPLOY_LOCK_FILE" || return 1
    grep -qE '^pid=[0-9]+ started=[0-9TZ:-]+ mode=deploy$' "$DEPLOY_LOCK_FILE" || return 1
}

@test "the lock is released when the run exits, also on failure" {
    run timeout 10 bash -c '
        source "$1"; tmp="$2"
        MARKER_FILE="$tmp/active-color"; CADDYFILE="$tmp/Caddyfile"
        COMPOSE_FILE="$tmp/compose.yml"; touch "$COMPOSE_FILE"
        ENV_FILE="$tmp/.env.prod"; touch "$ENV_FILE"
        validate_log_rotation_env() { :; }
        cmd_deploy() { error "boom"; }
        main
    ' _ "$DEPLOY_SH" "$TMP"
    [ "$status" -ne 0 ] || return 1
    [[ "$output" == *"boom"* ]] || return 1
    # Nobody holds it now: the test can take it without waiting.
    exec 8<> "$DEPLOY_LOCK_FILE"
    flock -n 8 || return 1
}

@test "a lock file that cannot be opened stops a mutating run (fail closed)" {
    export DEPLOY_LOCK_FILE="$TMP/no-such-dir-file/deploy.lock"
    touch "$TMP/no-such-dir-file"   # a FILE where the directory should be
    run_main
    [ "$status" -ne 0 ] || return 1
    [[ "$output" == *"$DEPLOY_LOCK_FILE"* ]] || return 1
    [ ! -e "$TMP/sentinel" ] || return 1
}

@test "the lock defaults to a path outside the source tree, next to the marker" {
    run bash -c 'unset DEPLOY_LOCK_FILE; source "$1"; echo "$DEPLOY_LOCK_FILE"' _ "$DEPLOY_SH"
    [ "$status" -eq 0 ] || return 1
    [ "$output" = "/opt/kagura-memory/deploy.lock" ] || return 1
}
