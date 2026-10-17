#!/usr/bin/env bats
# =============================================================================
# deploy.sh's format check for the container log rotation knobs (#2025):
# API_LOG_MAX_SIZE / LOG_MAX_SIZE (`<number><k|m|g>`) and API_LOG_MAX_FILE /
# LOG_MAX_FILE (a positive whole number). Docker rejects a bad json-file option
# only when it creates a container, so a typo would otherwise surface halfway
# through a deploy, or at the next reboot for the services deploy.sh never
# recreates. The check reads the value compose uses — the shell environment
# first, then .env.prod — and runs in main() before any container is touched.
#
# The script is sourced (BASH_SOURCE guard); no docker, no network.
# =============================================================================

setup() {
    # BATS_TEST_TMPDIR only exists on bats >= 1.4 (same fallback as
    # deploy_image_source.bats).
    if [ -n "${BATS_TEST_TMPDIR:-}" ]; then
        TMPD="$BATS_TEST_TMPDIR"
        TMPD_OWNED=0
    else
        TMPD="$(mktemp -d)"
        TMPD_OWNED=1
    fi
    unset API_LOG_MAX_SIZE API_LOG_MAX_FILE LOG_MAX_SIZE LOG_MAX_FILE

    # shellcheck disable=SC1090
    source "$BATS_TEST_DIRNAME/../deploy.sh"
    set +u

    ENV_FILE="$TMPD/.env.prod"
    printf 'DB_PASSWORD=test-pw\n' > "$ENV_FILE"
}

teardown() {
    if [ "${TMPD_OWNED:-0}" = "1" ] && [ -n "${TMPD:-}" ]; then
        rm -rf "$TMPD"
    fi
    return 0
}

@test "unset knobs pass (compose defaults apply)" {
    run validate_log_rotation_env
    [ "$status" -eq 0 ]
}

@test "valid values in .env.prod pass, in the quoted and commented forms too" {
    cat >> "$ENV_FILE" <<'EOF'
API_LOG_MAX_SIZE=200m
API_LOG_MAX_FILE="7"
LOG_MAX_SIZE='1g'
LOG_MAX_FILE=5   # keep five
EOF
    run validate_log_rotation_env
    [ "$status" -eq 0 ]
}

@test "an empty value passes (compose's :- default applies)" {
    printf 'API_LOG_MAX_SIZE=\nLOG_MAX_FILE=""\n' >> "$ENV_FILE"
    run validate_log_rotation_env
    [ "$status" -eq 0 ]
}

@test "a size without a k/m/g unit fails and names the variable" {
    for bad in 50 50mb 50M 1.5g m -1m; do
        printf 'LOG_MAX_SIZE=%s\n' "$bad" > "$ENV_FILE"
        run validate_log_rotation_env
        [ "$status" -ne 0 ] || { echo "accepted LOG_MAX_SIZE=$bad"; return 1; }
        [[ "$output" == *"LOG_MAX_SIZE must be"* ]]
    done
}

@test "a file count that is not a positive whole number fails and names the variable" {
    for bad in 0 03 -1 3x 1.5 three; do
        printf 'API_LOG_MAX_FILE=%s\n' "$bad" > "$ENV_FILE"
        run validate_log_rotation_env
        [ "$status" -ne 0 ] || { echo "accepted API_LOG_MAX_FILE=$bad"; return 1; }
        [[ "$output" == *"API_LOG_MAX_FILE must be"* ]]
    done
}

@test "the last assignment in .env.prod wins" {
    printf 'API_LOG_MAX_SIZE=bad\nAPI_LOG_MAX_SIZE=100m\n' >> "$ENV_FILE"
    run validate_log_rotation_env
    [ "$status" -eq 0 ]
}

@test "the shell environment wins over .env.prod, as in compose" {
    printf 'API_LOG_MAX_SIZE=bad\n' >> "$ENV_FILE"
    API_LOG_MAX_SIZE=100m run validate_log_rotation_env
    [ "$status" -eq 0 ]

    printf 'API_LOG_MAX_SIZE=100m\n' > "$ENV_FILE"
    LOG_MAX_FILE=0 run validate_log_rotation_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"LOG_MAX_FILE must be"* ]]
}

@test "main() runs the check before dispatching to any command" {
    body="$(sed -n '/^main() {/,/^}/p' "$BATS_TEST_DIRNAME/../deploy.sh")"
    check_at="$(grep -n 'validate_log_rotation_env' <<< "$body" | head -n 1 | cut -d: -f1)"
    case_at="$(grep -nE '^    case "\$\{1:-\}" in' <<< "$body" | cut -d: -f1)"
    [ -n "$check_at" ]
    [ -n "$case_at" ]
    [ "$check_at" -lt "$case_at" ]
}
