#!/usr/bin/env bats
# =============================================================================
# #1482: after the marker is republished, deploy.sh must PROVE the new color's
# container sees it through its single-file bind mount — before Caddy moves.
#
# A single-file bind mount resolves to an inode at container start. deploy.sh
# writes in place (cp), so the mount stays current; but an editor save, a
# `sed -i`, a backup restore or an older `mv` replaces the inode, and from
# then on the container answers with the color it booted with while the host
# file says otherwise. The host-side read-back in write_marker cannot see that.
# GET /api/v1/workers/active-color, asked THROUGH the container, can.
#
# `dc` (the compose wrapper) is overridden to return a canned JSON body; no
# docker, no network. The token stays inside the container in the real path.
# =============================================================================

DEPLOY_SH="$BATS_TEST_DIRNAME/../deploy.sh"

setup() {
    [ -r "$DEPLOY_SH" ] || return 1
    TMP="$(mktemp -d)"
    export BODY_FILE="$TMP/body" DC_RC_FILE="$TMP/rc" CALLS="$TMP/calls"
    : > "$CALLS"
    echo 0 > "$DC_RC_FILE"

    # shellcheck disable=SC1090
    source "$DEPLOY_SH"
    trap - EXIT
    set +u
    MARKER_FILE="$TMP/active-color"
    COMPOSE_FILE="$TMP/compose.yml"
    ENV_FILE="$TMP/.env"

    # dc() is a function in the script; override it to answer like the API.
    dc() {
        echo "dc $*" >> "$CALLS"
        if [ "$(cat "$DC_RC_FILE")" -ne 0 ]; then
            return "$(cat "$DC_RC_FILE")"
        fi
        cat "$BODY_FILE"
    }
}

teardown() {
    [ -n "${TMP:-}" ] && rm -rf "$TMP"
}

@test "a container that sees the new color on both fields passes" {
    printf '{"active_color":"green","responding_color":"green"}' > "$BODY_FILE"
    run verify_active_color_endpoint green
    [ "$status" -eq 0 ] || return 1
    [[ "$output" == *"marker mount is live"* ]] || return 1
}

@test "the probe runs INSIDE the new color's container, with the token from its env" {
    printf '{"active_color":"green","responding_color":"green"}' > "$BODY_FILE"
    run verify_active_color_endpoint green
    [ "$status" -eq 0 ] || return 1
    grep -q '^dc exec -T api-green sh -c ' "$CALLS" || return 1
    grep -q 'WORKER_SERVICE_TOKEN' "$CALLS" || return 1
    grep -q '/api/v1/workers/active-color' "$CALLS" || return 1
}

@test "a stale marker view (pinned inode) aborts BEFORE Caddy is switched" {
    # The host file says green; the container still reads the inode it booted
    # with, which says blue. That is the #1482 failure, now caught.
    printf '{"active_color":"blue","responding_color":"green"}' > "$BODY_FILE"
    run verify_active_color_endpoint green
    [ "$status" -ne 0 ] || return 1
    [[ "$output" == *"active_color='blue'"* ]] || return 1
    [[ "$output" == *"pinned to an inode"* ]] || return 1
    [[ "$output" == *"Caddy has NOT been switched"* ]] || return 1
    [[ "$output" == *"--force-recreate api-blue api-green"* ]] || return 1
}

@test "a responding_color that is not the probed color fails too" {
    # Guards against a substring/always-true comparison: the marker may be
    # right while the container answering is not the one we asked for.
    printf '{"active_color":"green","responding_color":"blue"}' > "$BODY_FILE"
    run verify_active_color_endpoint green
    [ "$status" -ne 0 ] || return 1
}

@test "a 503 (marker mount unreadable) names the recreate recovery" {
    echo 22 > "$DC_RC_FILE"   # curl -f exit code on a 5xx
    run verify_active_color_endpoint green
    [ "$status" -ne 0 ] || return 1
    [[ "$output" == *"did not answer"* ]] || return 1
    [[ "$output" == *"--force-recreate api-blue api-green"* ]] || return 1
}

@test "an unparseable body fails closed rather than matching an empty string" {
    printf 'not json' > "$BODY_FILE"
    run verify_active_color_endpoint green
    [ "$status" -ne 0 ] || return 1
}

@test "json_string_field reads exactly the named field" {
    body='{"active_color": "green", "responding_color":"blue"}'
    [ "$(json_string_field "$body" active_color)" = "green" ] || return 1
    [ "$(json_string_field "$body" responding_color)" = "blue" ] || return 1
    [ -z "$(json_string_field "$body" nope)" ] || return 1
}

@test "both flip paths verify the endpoint right after writing the marker" {
    # Pin the ordering: the check must sit between write_marker and the Caddy
    # switch on BOTH deploy and rollback, or a stale view goes live.
    for fn in cmd_deploy cmd_rollback; do
        body="$(declare -f "$fn")"
        w="$(printf '%s\n' "$body" | grep -n 'write_marker "\$inactive"' | head -n1 | cut -d: -f1)"
        v="$(printf '%s\n' "$body" | grep -n 'verify_active_color_endpoint "\$inactive"' | head -n1 | cut -d: -f1)"
        c="$(printf '%s\n' "$body" | grep -n 'generate_caddyfile "api-\${inactive}"' | head -n1 | cut -d: -f1)"
        [ -n "$w" ] && [ -n "$v" ] && [ -n "$c" ] || return 1
        [ "$w" -lt "$v" ] && [ "$v" -lt "$c" ] || return 1
    done
}

@test "write_marker leaves the marker world-readable, in place" {
    printf 'blue\n' > "$MARKER_FILE"
    chmod 0600 "$MARKER_FILE"
    before="$(stat -c '%i' "$MARKER_FILE")"
    write_marker green
    [ "$(stat -c '%a' "$MARKER_FILE")" = "644" ] || return 1
    [ "$(stat -c '%i' "$MARKER_FILE")" = "$before" ] || return 1
    [ "$(cat "$MARKER_FILE")" = "green" ] || return 1
}

@test "get_active_color refuses a marker the API containers could not read" {
    printf 'blue\n' > "$MARKER_FILE"
    chmod 0600 "$MARKER_FILE"
    run get_active_color
    [ "$status" -ne 0 ] || return 1
    [[ "$output" == *"not world-readable"* ]] || return 1
    [[ "$output" == *"chmod 0644"* ]] || return 1
}
