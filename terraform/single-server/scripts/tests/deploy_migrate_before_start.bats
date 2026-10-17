#!/usr/bin/env bats
# =============================================================================
# #2048: cmd_deploy must migrate the database BEFORE the new color starts.
#
# The API lifespan starts background tasks the moment uvicorn boots. Migrating
# after the new color was up (the old Step 4 `dc exec ... alembic upgrade head`)
# let those tasks read the old schema, which on the v0.110.0 upgrade produced
# undefined-column errors from a startup backfill. These tests pin:
#   1. the one-off `dc run --rm --no-deps -T` migration runs before
#      `dc up` of api-<inactive>, and only once
#   2. a failing migration aborts before the new color is started
#   3. registry mode aborts before the migration, without a build (compose
#      run has no --no-build), when the image compose would run is not the one
#      Step 1 tagged, or is not present locally
#   4. --rollback never runs a migration
#
# deploy.sh guards `main` behind BASH_SOURCE, so the real cmd_deploy is driven
# with every step past the image/migrate/start calls stubbed out. No docker.
# =============================================================================

DEPLOY_SH="$BATS_TEST_DIRNAME/../deploy.sh"

setup() {
    [ -r "$DEPLOY_SH" ] || return 1
    # mktemp rather than $BATS_TEST_TMPDIR (bats < 1.4 leaves it unset).
    TMP="$(mktemp -d)"
    export CALLS="$TMP/calls"
    : > "$CALLS"

    # Realistic image store: `docker tag <ref> <name>` creates <name>, and
    # `docker image inspect <name>` succeeds only for a name that was tagged.
    export TAGGED="$TMP/tagged"
    : > "$TAGGED"
    mock_docker() {
        echo "docker $*" >> "$CALLS"
        case "${1:-}" in
            tag) echo "$3" >> "$TAGGED" ;;
            image) grep -qxF -- "$3" "$TAGGED" ;;
            *) return 0 ;;
        esac
    }
    export -f mock_docker
    export DOCKER=mock_docker

    # shellcheck disable=SC1090
    source "$DEPLOY_SH"
    trap - EXIT
    # Keep -e (bats relies on it); drop -u only.
    set +u

    COMPOSE_PROJECT="single-server"
    DRAIN_TIMEOUT=0
    MIGRATE_RC=0
    # What `dc config --images api-green` prints: the service's own image plus
    # its dependencies' (as real compose does for a service with depends_on).
    RESOLVED_IMAGES="redis:7-alpine
single-server-api-green
qdrant/qdrant:v1.19.1"

    # Record every compose call; the migration exits with $MIGRATE_RC.
    dc() {
        echo "dc $*" >> "$CALLS"
        case "${1:-}" in
            run) return "$MIGRATE_RC" ;;
            config) printf '%s\n' "$RESOLVED_IMAGES" ;;
        esac
        return 0
    }
    # `docker update` in cmd_deploy calls docker directly, not via $DOCKER.
    docker() { echo "docker $*" >> "$CALLS"; }

    # Stub everything that would touch the host. Active color is blue, so the
    # deploy targets green.
    get_active_color() { echo blue; }
    get_inactive_color() { echo green; }
    check_marker_matches_live() { return 0; }
    wait_for_readiness() { echo "readiness $1" >> "$CALLS"; }
    write_marker() { echo "marker $1" >> "$CALLS"; }
    verify_active_color_endpoint() { :; }
    generate_caddyfile() { echo "caddy $1" >> "$CALLS"; }
    reload_caddy() { :; }
    verify_workers_blocked() { :; }
    verify_internal_blocked() { :; }
    restart_bridge_worker() { :; }
    bridge_worker_is_running() { return 1; }
    report_bridge_state() { :; }
    is_container_running() { return 1; }
    ensure_rollback_image() { :; }
}

teardown() {
    [ -n "${TMP:-}" ] && rm -rf "$TMP"
}

# Line number of the first call matching $1 in $CALLS (empty if absent).
line_of() { grep -n -F -- "$1" "$CALLS" | head -1 | cut -d: -f1; }

@test "the migration runs before the new color is started" {
    run cmd_deploy
    [ "$status" -eq 0 ] || return 1

    local migrate up ready
    migrate="$(line_of "dc run --rm --no-deps -T api-green alembic upgrade head")"
    up="$(line_of "dc up -d --no-deps api-green")"
    ready="$(line_of "readiness green")"
    [ -n "$migrate" ] && [ -n "$up" ] && [ -n "$ready" ] || return 1
    [ "$migrate" -lt "$up" ] || return 1
    [ "$up" -lt "$ready" ] || return 1
}

@test "the migration runs exactly once and never via dc exec" {
    run cmd_deploy
    [ "$status" -eq 0 ] || return 1
    [ "$(grep -c alembic "$CALLS")" -eq 1 ] || return 1
    ! grep -q "dc exec" "$CALLS" || return 1
}

@test "a failing migration aborts before the new color is started" {
    MIGRATE_RC=1
    run cmd_deploy
    [ "$status" -ne 0 ] || return 1
    [[ "$output" == *"alembic upgrade head failed; api-green was not started"* ]] || return 1
    grep -q "alembic upgrade head" "$CALLS" || return 1
    ! grep -q "dc up" "$CALLS" || return 1
    ! grep -q "marker green" "$CALLS" || return 1
    ! grep -q "caddy" "$CALLS" || return 1
}

registry_env() {
    KAGURA_IMAGE_SOURCE="registry"
    KAGURA_IMAGE_REPO="registry.example.com/kagura-api"
    KAGURA_IMAGE_TAG="v0.65.0"
}

@test "registry mode aborts before the migration when compose resolves a different image name" {
    registry_env
    # e.g. COMPOSE_PROJECT_NAME set only in .env.prod: Step 1 tags
    # single-server-api-green, compose would run (and build) prod-api-green.
    RESOLVED_IMAGES="redis:7-alpine
prod-api-green"

    run cmd_deploy
    [ "$status" -ne 0 ] || return 1
    [[ "$output" == *"does not resolve api-green to single-server-api-green"* ]] || return 1
    grep -q "docker tag registry.example.com/kagura-api:v0.65.0 single-server-api-green" "$CALLS" || return 1
    ! grep -q "alembic" "$CALLS" || return 1
    ! grep -q "dc build" "$CALLS" || return 1
    ! grep -q "dc up" "$CALLS" || return 1
}

@test "registry mode aborts before the migration when the local image is gone" {
    registry_env
    # The tag "succeeds" but the store never gets the name (e.g. removed by a
    # concurrent prune between Step 1 and Step 2).
    mock_docker_lossy() {
        echo "docker $*" >> "$CALLS"
        [ "${1:-}" = "image" ] && return 1
        return 0
    }
    DOCKER=mock_docker_lossy

    run cmd_deploy
    [ "$status" -ne 0 ] || return 1
    [[ "$output" == *"single-server-api-green is not present locally"* ]] || return 1
    ! grep -q "alembic" "$CALLS" || return 1
    ! grep -q "dc build" "$CALLS" || return 1
    ! grep -q "dc up" "$CALLS" || return 1
}

@test "registry mode with the local image present migrates then starts with --no-build" {
    registry_env

    run cmd_deploy
    [ "$status" -eq 0 ] || return 1
    local migrate up
    migrate="$(line_of "dc run --rm --no-deps -T api-green alembic upgrade head")"
    up="$(line_of "dc up -d --no-deps --no-build api-green")"
    [ -n "$migrate" ] && [ -n "$up" ] && [ "$migrate" -lt "$up" ] || return 1
}

@test "rollback never runs a migration" {
    run cmd_rollback
    [ "$status" -eq 0 ] || return 1
    # Guard against a vacuous pass: rollback did start the previous color.
    grep -q "dc up -d --no-deps api-green" "$CALLS" || return 1
    ! grep -q "alembic" "$CALLS" || return 1
    ! grep -q "dc run" "$CALLS" || return 1
}

@test "every 'dc run' in deploy.sh passes --no-deps" {
    # Static sibling of deploy_no_deps_guard.bats for the one-off migration.
    run bash -o pipefail -c 'grep -nE "dc run" "$1" | grep -v -E "^[0-9]+:[[:space:]]*#" | grep -v -- "--no-deps"' _ "$DEPLOY_SH"
    [ "$status" -eq 1 ] || return 1
    [ -z "$output" ] || return 1
}
