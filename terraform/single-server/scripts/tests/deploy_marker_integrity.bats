#!/usr/bin/env bats
# =============================================================================
# #1448: the active-color marker must never quietly disagree with reality.
#
# Production ran 65 hours with the marker naming `blue` while no blue container
# existed. Every control-plane call reached the API only through kagura-bridge's
# connect-level cross-color fallback (kagura-ai/kagura-bridge#211) — 4162 times —
# turning a safety net into the normal path and filling 99.7% of the bridge log
# with one warning.
#
# The issue proposed reordering the switch (start → health → marker → drain).
# That order was ALREADY implemented (deploy Step 2→7, and rollback starts and
# waits for readiness before writing the marker), so reordering would have been
# a no-op. What was missing:
#
#   1. a missing marker resolved to "blue" silently, so every reader agreed on a
#      color nobody selected;
#   2. nothing ever compared the marker against which container was running.
#
# deploy.sh guards `main` behind BASH_SOURCE, so these call the real functions
# rather than grepping for them. No docker, no network.
# =============================================================================

DEPLOY_SH="$BATS_TEST_DIRNAME/../deploy.sh"

setup() {
    [ -r "$DEPLOY_SH" ] || return 1
    TMP="$(mktemp -d)"
    # shellcheck disable=SC1090
    source "$DEPLOY_SH"
    MARKER_FILE="$TMP/active-color"
    # Neutralize the EXIT trap the script installs: bats runs each assertion in
    # its own subshell, and a stray final-status line would pollute $output.
    trap - EXIT
    # deploy.sh sets `-euo pipefail` and sourcing leaves those on here.
    #
    # `-e` must STAY ON: bats detects a failing test through it, so `set +e`
    # makes every assertion advisory — a body ending in `false || return 1`
    # still reports `ok`. (Confirmed the hard way while checking these guards
    # are not vacuous.) Only `-u` is dropped, because an unbound-variable death
    # inside a test aborts the file instead of failing one case.
    #
    # Assertions still carry an explicit `|| return 1` so the intent survives
    # even if someone later relaxes `-e`.
    #
    # Known rough edge: when one of these guards DOES catch a regression, `-e`
    # kills the body before bats can name it, so the run reports
    # "Executed 7 instead of expected 11" rather than `not ok 1 ...`. The run
    # still exits non-zero, so CI fails correctly — but if you see that warning,
    # the missing numbers are the failures.
    set +u
}

teardown() {
    [ -n "${TMP:-}" ] && rm -rf "$TMP"
}

@test "a missing marker is an error, not a silent 'blue'" {
    run get_active_color
    [ "$status" -ne 0 ] || return 1
    [[ "$output" == *"missing or empty"* ]] || return 1
    # It must not answer with a color it invented.
    [[ "$output" != *$'\n'"blue" ]] || return 1
}

@test "an empty marker is an error too (truncated by an unclean shutdown)" {
    : > "$MARKER_FILE"
    run get_active_color
    [ "$status" -ne 0 ] || return 1
    [[ "$output" == *"missing or empty"* ]] || return 1
}

@test "the error names the file and shows how to recover" {
    run get_active_color
    [[ "$output" == *"$MARKER_FILE"* ]] || return 1
    [[ "$output" == *"docker ps"* ]] || return 1
}

@test "a valid marker still round-trips" {
    echo "green" > "$MARKER_FILE"
    run get_active_color
    [ "$status" -eq 0 ] || return 1
    [ "$output" = "green" ] || return 1
}

@test "an invalid marker value is still rejected" {
    echo "purple" > "$MARKER_FILE"
    run get_active_color
    [ "$status" -ne 0 ] || return 1
    [[ "$output" == *"invalid value"* ]] || return 1
}

@test "a marker naming a color that is not running is reported" {
    is_container_running() { return 1; }
    run check_marker_matches_live "blue"
    [ "$status" -ne 0 ] || return 1
    [[ "$output" == *"is NOT running"* ]] || return 1
}

@test "the report points at the color that IS running" {
    # blue is dead, green is alive — the operator needs to be told which.
    is_container_running() { [ "$1" = "green" ]; }
    run check_marker_matches_live "blue"
    [ "$status" -ne 0 ] || return 1
    [[ "$output" == *"kagura-api-green IS running"* ]] || return 1
}

@test "a marker that matches the running color is silent" {
    is_container_running() { return 0; }
    run check_marker_matches_live "blue"
    [ "$status" -eq 0 ] || return 1
    [ -z "$output" ] || return 1
}

@test "deploy refuses to run from a marker that does not match reality" {
    # Static: the guard must sit in cmd_deploy, before the colors it derives are
    # used. Calling cmd_deploy for real would start containers.
    run bash -o pipefail -c 'sed -n "/^cmd_deploy()/,/^}/p" "$1" | grep -c "check_marker_matches_live"' _ "$DEPLOY_SH"
    [ "$status" -eq 0 ] || return 1
    [ "$output" -ge 1 ] || return 1
}

@test "status reports the mismatch instead of dying on it" {
    run bash -o pipefail -c 'sed -n "/^cmd_status()/,/^}/p" "$1" | grep -c "check_marker_matches_live"' _ "$DEPLOY_SH"
    [ "$status" -eq 0 ] || return 1
    [ "$output" -ge 1 ] || return 1
}

@test "no reader falls back to a default color" {
    # The regression this whole file exists for: `|| echo "blue"` (or any
    # variant) reintroduces the silent agreement on an unselected color.
    run bash -o pipefail -c 'grep -nE "cat .*MARKER_FILE.*\|\|" "$1"' _ "$DEPLOY_SH"
    [ "$status" -eq 1 ] || return 1
    [ -z "$output" ] || return 1
}

@test "an existing but unreadable marker gets its own error, not a bare abort" {
    # A directory passes `-s` (non-empty) but `cat` fails on it — the same shape
    # as a root-owned file or an IO error, without needing to drop privileges.
    # Before this branch, `set -e` killed the run inside the command
    # substitution with no message of ours (Copilot review on #1462).
    MARKER_FILE="$TMP/as-a-directory"
    mkdir -p "$MARKER_FILE/x"
    run get_active_color
    [ "$status" -ne 0 ] || return 1
    [[ "$output" == *"could not be read"* ]] || return 1
    [[ "$output" == *"ls -l"* ]] || return 1
}

@test "recovery instructions are safe to paste" {
    # `echo <blue|green> > file` looks like a placeholder but `<` and `|` are
    # shell operators — pasted mid-incident it redirects and pipes instead of
    # writing (Copilot review on #1462). Both the error text and the README
    # must spell the two commands out.
    run bash -o pipefail -c 'grep -nE "echo <" "$1"' _ "$DEPLOY_SH"
    [ "$status" -eq 1 ] || return 1
    [ -z "$output" ] || return 1

    run get_active_color
    [[ "$output" == *"echo blue"* ]] || return 1
    [[ "$output" == *"echo green"* ]] || return 1
}

@test "the README recovery snippet is safe to paste too" {
    readme="$BATS_TEST_DIRNAME/../../README.md"
    [ -r "$readme" ] || return 1
    run bash -o pipefail -c 'grep -nE "echo <" "$1"' _ "$readme"
    [ "$status" -eq 1 ] || return 1
    [ -z "$output" ] || return 1
}
