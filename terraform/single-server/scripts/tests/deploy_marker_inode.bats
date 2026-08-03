#!/usr/bin/env bats
# =============================================================================
# #1480: the active-color marker must be published WITHOUT changing its inode.
#
# The marker is bind-mounted into co-resident stacks as a SINGLE FILE
# (kagura-bridge mounts it read-only to follow the active color). A single-file
# bind mount resolves to an inode at container start, so replacing the file via
# `mv` publishes a new inode that the running container never sees — it keeps
# reading the color it booted with.
#
# Measured on docker 29.3.1 with a mount of this exact shape:
#   mv + container running  -> container still reads the OLD color
#   mv + `docker restart`   -> container reads the new color
#   cp + container running  -> container reads the new color, with NO restart
#
# That is why kagura-bridge was forced onto a static KMC_INTERNAL_URL pin, and
# why #1477's Step 6b `docker restart` could not re-point it (a restart cannot
# re-read an .env). Publishing in place removes the original cause.
#
# These tests assert the INODE, not the file contents — contents were already
# correct under `mv`, which is exactly why the bug survived review. No docker
# and no network: the property is a filesystem property.
# =============================================================================

DEPLOY_SH="$BATS_TEST_DIRNAME/../deploy.sh"

setup() {
    [ -r "$DEPLOY_SH" ] || return 1
    TMP="$(mktemp -d)"

    # shellcheck disable=SC1090
    source "$DEPLOY_SH"
    MARKER_FILE="$TMP/active-color"

    # Neutralize the EXIT trap deploy.sh installs — bats runs each assertion in
    # its own subshell and a stray final-status line would pollute $output.
    trap - EXIT

    # `-e` must STAY ON: bats detects a failing test through it, so `set +e`
    # would make every assertion advisory. Only `-u` is dropped, because an
    # unbound-variable death aborts the whole file instead of failing one case.
    set +u
}

teardown() {
    [ -n "${TMP:-}" ] && rm -rf "$TMP"
}

inode_of() { stat -c '%i' "$1"; }

@test "write_marker publishes the new color" {
    echo "blue" > "$MARKER_FILE"
    write_marker "green"
    [ "$(cat "$MARKER_FILE")" = "green" ] || return 1
}

@test "write_marker does NOT change the marker's inode (the #1480 property)" {
    echo "blue" > "$MARKER_FILE"
    local before after
    before="$(inode_of "$MARKER_FILE")"
    write_marker "green"
    after="$(inode_of "$MARKER_FILE")"
    # A single-file bind mount tracks the inode. Change it and every running
    # consumer is frozen on the old color.
    [ "$before" = "$after" ] || return 1
}

@test "the inode is stable across repeated flips" {
    echo "blue" > "$MARKER_FILE"
    local first
    first="$(inode_of "$MARKER_FILE")"
    write_marker "green"
    write_marker "blue"
    write_marker "green"
    [ "$(inode_of "$MARKER_FILE")" = "$first" ] || return 1
    [ "$(cat "$MARKER_FILE")" = "green" ] || return 1
}

@test "write_marker leaves no .tmp file behind" {
    echo "blue" > "$MARKER_FILE"
    write_marker "green"
    [ ! -e "${MARKER_FILE}.tmp" ] || return 1
}

@test "the result round-trips through get_active_color" {
    echo "blue" > "$MARKER_FILE"
    write_marker "green"
    run get_active_color
    [ "$status" -eq 0 ] || return 1
    [ "$output" = "green" ] || return 1
}

@test "no marker writer anywhere in the script uses mv (regression guard)" {
    # The bug was not that write_marker was wrong — it did not exist. It was two
    # open-coded `mv` pairs, in cmd_deploy and cmd_rollback. Any reintroduction,
    # in any function, re-freezes every single-file consumer.
    #
    # Deliberately NOT wrapped in `bash -c "..."`: inside a double-quoted
    # bash -c payload, `\$` collapses to a bare `$`, which ERE then reads as an
    # end-of-line ANCHOR. The pattern silently stops matching and the guard
    # passes forever. (Caught by mutating deploy.sh back to `mv` and watching
    # this test stay green.) Single quotes here reach grep untouched.
    local hits
    hits="$(grep -cE 'mv +"?\$\{?MARKER_FILE' "$DEPLOY_SH" || true)"
    [ "$hits" = "0" ] || return 1
}

@test "the mv guard actually matches an mv line (guard not vacuous)" {
    # Pin the pattern against a known-bad sample, so the guard above can never
    # rot into an anchor that matches nothing.
    local sample="$TMP/sample.sh"
    printf '%s\n' '    mv "${MARKER_FILE}.tmp" "$MARKER_FILE"' > "$sample"
    local hits
    hits="$(grep -cE 'mv +"?\$\{?MARKER_FILE' "$sample" || true)"
    [ "$hits" = "1" ] || return 1
}

@test "both color-switching paths publish through write_marker" {
    # cmd_deploy Step 5 and cmd_rollback both flip the color; both must go
    # through the helper rather than open-coding the write again.
    local d r
    d="$(sed -n '/^cmd_deploy()/,/^}/p' "$DEPLOY_SH" | grep -cE '^ *write_marker ' || true)"
    r="$(sed -n '/^cmd_rollback()/,/^}/p' "$DEPLOY_SH" | grep -cE '^ *write_marker ' || true)"
    [ "$d" -ge 1 ] || return 1
    [ "$r" -ge 1 ] || return 1
}

@test "generate_caddyfile still uses cp, so the two writers agree (guard not vacuous)" {
    # The Caddyfile writer already had this fix and its comment is the precedent
    # this change follows. If someone 'simplifies' it back to mv, the same class
    # of bug returns for Caddy's config.
    local hits
    hits="$(sed -n '/^generate_caddyfile()/,/^}/p' "$DEPLOY_SH" | grep -cF 'cp "$CADDYFILE.tmp"' || true)"
    [ "$hits" -ge 1 ] || return 1
}

@test "a failed cp reports that the marker may be truncated" {
    # `cp` opens the destination with O_TRUNC, so a mid-write failure damages
    # the LIVE marker — the real cost of preserving the inode. The operator must
    # be told that, or they cannot tell a stale marker from a broken one.
    #
    # Deterministic cp failure without needing a full disk or a uid trick:
    # destination is a directory ALREADY CONTAINING a directory of the source's
    # basename, so `cp src dir` cannot write dir/src. Staging still succeeds, so
    # this reaches the cp branch rather than the staging branch.
    MARKER_FILE="$TMP/x"
    mkdir -p "$TMP/x/x.tmp"
    run write_marker "green"
    [ "$status" -ne 0 ] || return 1
    [[ "$output" == *"TRUNCATED"* ]] || return 1
}

@test "the truncation error NAMES the previous color, so recovery is possible" {
    # Behavioural, not a grep for `local previous`: a static check still passes
    # when the captured value is never actually read, and being able to name the
    # old color is the only thing that makes the message actionable.
    #
    # Injection: marker is readable (0444) so `previous` is captured, but not
    # writable, so `cp` fails while staging into $TMP still succeeds. That is
    # the cp branch specifically, with a real previous value in hand.
    if [ "$(id -u)" -eq 0 ]; then
        skip "a 0444 file is still writable by root, so cp would not fail"
    fi
    MARKER_FILE="$TMP/ro-marker"
    printf 'blue\n' > "$MARKER_FILE"
    chmod 0444 "$MARKER_FILE"

    run write_marker "green"

    chmod 0644 "$MARKER_FILE"
    [ "$status" -ne 0 ] || return 1
    [[ "$output" == *"TRUNCATED"* ]] || return 1
    # The whole point: tell the operator what to put back.
    [[ "$output" == *"blue"* ]] || return 1
    [[ "$output" != *"unknown"* ]] || return 1
}

@test "a marker path that cannot be staged is a loud error" {
    # Parent directory does not exist, so the staging redirect fails. Chosen
    # over a permission trick because it behaves the same as root and non-root,
    # and CI runners differ.
    MARKER_FILE="$TMP/no-such-dir/active-color"
    run write_marker "green"
    [ "$status" -ne 0 ] || return 1
    [[ "$output" == *"Could not stage"* ]] || return 1
}

@test "a marker path that is a DIRECTORY fails instead of silently no-op'ing" {
    # `cp file dir` exits 0 and copies INTO the directory, so cp's status alone
    # would report success while the published color never changed. The
    # read-back is what catches it (here via get_active_color's own guard).
    MARKER_FILE="$TMP/as-a-directory"
    mkdir -p "$MARKER_FILE"
    run write_marker "green"
    [ "$status" -ne 0 ] || return 1
    [[ "$output" == *"could not be read"* ]] || return 1
}

@test "write_marker verifies the published value, not just the exit code" {
    # Guard against dropping the read-back: the whole lesson of #1480 is that a
    # step must prove its outcome. Assert the mechanism is present.
    run bash -o pipefail -c 'sed -n "/^write_marker()/,/^}/p" "$1" | grep -c "reads back as"' _ "$DEPLOY_SH"
    [ "$output" -ge 1 ] || return 1
}

@test "write_marker does not reintroduce the forbidden cat-with-fallback shape" {
    # #1448's guard (deploy_marker_integrity.bats) bans `cat ... MARKER_FILE ... ||`
    # because that shape is how a silent default color creeps back in. Adding a
    # read-back is exactly when someone would reach for it.
    run bash -o pipefail -c 'sed -n "/^write_marker()/,/^}/p" "$1" | grep -cE "cat .*MARKER_FILE.*\|\|"' _ "$DEPLOY_SH"
    [ "$output" = "0" ] || return 1
}
