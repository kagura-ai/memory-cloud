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
    # Any `dc up -d` whose first argument does not start with a dash and
    # targets an api color is a regression of the #1302 guard.
    run grep -nE 'dc up -d +"api-' "$DEPLOY_SH"
    [ "$status" -eq 1 ]
    [ -z "$output" ]
}

@test "api colors are started via the --no-deps form (guard not vacuous)" {
    run bash -c 'grep -cE "dc up -d --no-deps \"api-" "$1"' _ "$DEPLOY_SH"
    [ "$status" -eq 0 ]
    [ "$output" -ge 2 ]
}
