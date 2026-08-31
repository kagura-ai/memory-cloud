#!/usr/bin/env bats
# =============================================================================
# Regression guard for the #1302 postgres footgun: every `dc up -d` that
# targets an api color (deploy Step 2, rollback restart) must pass --no-deps,
# so a routine blue-green deploy can never recreate shared stateful services
# (postgres/redis/qdrant) whose compose config has drifted from the running
# containers — e.g. the PG18 image + fresh-volume change, where a dependency
# recreate would silently serve an empty database. cmd_deploy_web has carried
# the same guard since #643/#672. Static conformance — no docker, no network.
# =============================================================================

DEPLOY_SH="$BATS_TEST_DIRNAME/../deploy.sh"

@test "no api-targeting 'dc up -d' without --no-deps" {
    # Any non-comment `dc up -d` line that targets an api service (quoted or
    # unquoted) and lacks --no-deps is a regression of the #1302 guard.
    # The readability guard + pipefail keep the test from passing vacuously
    # if deploy.sh moves (a grep read error would otherwise be masked by the
    # trailing grep -v exiting 1 on empty input).
    [ -r "$DEPLOY_SH" ]
    run bash -o pipefail -c 'grep -nE "dc up -d" "$1" | grep -v -E "^[0-9]+:[[:space:]]*#" | grep -E "api-" | grep -v -- "--no-deps"' _ "$DEPLOY_SH"
    [ "$status" -eq 1 ]
    [ -z "$output" ]
}

@test "every 'dc up -d' inside dc_up_service passes --no-deps" {
    # #1513 moved the api-color start behind dc_up_service (it adds --no-build
    # in registry mode), so the guard follows the indirection: the helper is now
    # the single place that can drop --no-deps for BOTH api colors at once.
    [ -r "$DEPLOY_SH" ]
    run bash -o pipefail -c '
        sed -n "/^dc_up_service() {/,/^}/p" "$1" | grep -E "dc up -d" | grep -v -- "--no-deps"
    ' _ "$DEPLOY_SH"
    [ "$status" -eq 1 ]
    [ -z "$output" ]
}

@test "dc_up_service actually contains 'dc up -d' calls (guard not vacuous)" {
    # Without this, the test above passes trivially if the helper is renamed
    # or its body stops calling dc up -d.
    [ -r "$DEPLOY_SH" ]
    run bash -o pipefail -c '
        sed -n "/^dc_up_service() {/,/^}/p" "$1" | grep -cE "dc up -d --no-deps"
    ' _ "$DEPLOY_SH"
    [ "$status" -eq 0 ]
    [ "$output" -ge 2 ]
}

@test "api colors are started through dc_up_service (guard not vacuous)" {
    # Both cmd_deploy (Step 2) and cmd_rollback must route through the helper;
    # a direct `dc up` for an api color would bypass the check above.
    [ -r "$DEPLOY_SH" ]
    run bash -o pipefail -c 'grep -cE "dc_up_service \"api-" "$1"' _ "$DEPLOY_SH"
    [ "$status" -eq 0 ]
    [ "$output" -ge 2 ]
}
