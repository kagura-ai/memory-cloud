#!/usr/bin/env bats
# =============================================================================
# Tests for the image-acquisition path added in #1513 (build vs registry).
#
# The split-host layout deploys a pre-built artifact on the app host instead
# of building from a source tree. These tests pin:
#   - "build" stays the default, so the single-host GCE deploy is unchanged
#   - "registry" pulls and re-tags, and refuses to run half-configured
#   - blue and green resolve to DIFFERENT local image names
#
# That last one is the load-bearing case: both colors build from the same
# context, so if they ever shared one tag, deploying the inactive color would
# repoint the active color's definition and `--rollback` would come back on the
# NEW image — a rollback that silently rolls nothing back.
#
# The script is sourced (it guards its entrypoint with a BASH_SOURCE check) and
# every docker call is captured through the $DOCKER indirection plus a dc()
# override. No docker, no network.
# =============================================================================

setup() {
    # BATS_TEST_TMPDIR only exists on bats >= 1.4; fall back so the suite is
    # runnable on the older bats that ships in some distro packages.
    TMPD="${BATS_TEST_TMPDIR:-$(mktemp -d)}"
    export CALLS="$TMPD/calls"
    : > "$CALLS"

    # Stub for the $DOCKER indirection: record argv, succeed.
    mock_docker() { echo "docker $*" >> "$CALLS"; }
    export -f mock_docker
    export DOCKER=mock_docker

    export CADDYFILE_TPL="$TMPD/Caddyfile.tpl"

    # shellcheck disable=SC1090
    source "$BATS_TEST_DIRNAME/../deploy.sh"
    set +u

    # dc() is a function in the script; override it to record instead of run.
    dc() { echo "dc $*" >> "$CALLS"; }

    # Pin the project name so the expected local tags are deterministic.
    COMPOSE_PROJECT="single-server"
}

@test "default image source is build" {
    [ "$KAGURA_IMAGE_SOURCE" = "build" ]
}

@test "build mode builds the target color and never pulls" {
    acquire_api_image "green"
    run cat "$CALLS"
    [[ "$output" == *"dc build api-green"* ]]
    [[ "$output" != *"docker pull"* ]]
}

@test "registry mode pulls the ref and re-tags it for the color" {
    KAGURA_IMAGE_SOURCE="registry"
    KAGURA_IMAGE_REPO="registry.example.com/kagura-api"
    KAGURA_IMAGE_TAG="v0.65.0"

    acquire_api_image "blue"

    run cat "$CALLS"
    [[ "$output" == *"docker pull registry.example.com/kagura-api:v0.65.0"* ]]
    [[ "$output" == *"docker tag registry.example.com/kagura-api:v0.65.0 single-server-api-blue"* ]]
    # Registry mode must not fall through to a build.
    [[ "$output" != *"dc build"* ]]
}

@test "blue and green re-tag to different local image names" {
    KAGURA_IMAGE_SOURCE="registry"
    KAGURA_IMAGE_REPO="registry.example.com/kagura-api"
    KAGURA_IMAGE_TAG="v0.65.0"

    acquire_api_image "blue"
    acquire_api_image "green"

    run cat "$CALLS"
    [[ "$output" == *"single-server-api-blue"* ]]
    [[ "$output" == *"single-server-api-green"* ]]
}

@test "registry mode aborts when the repo is unset" {
    KAGURA_IMAGE_SOURCE="registry"
    KAGURA_IMAGE_REPO=""
    KAGURA_IMAGE_TAG="v0.65.0"

    run acquire_api_image "blue"
    [ "$status" -ne 0 ]
    [[ "$output" == *"repo is unset"* ]]
}

@test "registry mode aborts when the tag is unset" {
    KAGURA_IMAGE_SOURCE="registry"
    KAGURA_IMAGE_REPO="registry.example.com/kagura-api"
    KAGURA_IMAGE_TAG=""

    run acquire_api_image "blue"
    [ "$status" -ne 0 ]
    [[ "$output" == *"tag is unset"* ]]
}

@test "an unknown image source aborts instead of guessing" {
    KAGURA_IMAGE_SOURCE="magic"

    run acquire_api_image "blue"
    [ "$status" -ne 0 ]
    [[ "$output" == *"Unknown KAGURA_IMAGE_SOURCE"* ]]
}

@test "web build mode keeps --no-cache" {
    acquire_web_image
    run cat "$CALLS"
    [[ "$output" == *"dc build --no-cache web"* ]]
}

@test "web registry mode pulls the web repo and tags it for the web service" {
    KAGURA_IMAGE_SOURCE="registry"
    KAGURA_WEB_IMAGE_REPO="registry.example.com/kagura-web"
    KAGURA_WEB_IMAGE_TAG="v0.65.0"

    acquire_web_image

    run cat "$CALLS"
    [[ "$output" == *"docker pull registry.example.com/kagura-web:v0.65.0"* ]]
    [[ "$output" == *"docker tag registry.example.com/kagura-web:v0.65.0 single-server-web"* ]]
}

@test "the web path does not borrow the API image repo" {
    KAGURA_IMAGE_SOURCE="registry"
    KAGURA_IMAGE_REPO="registry.example.com/kagura-api"
    KAGURA_IMAGE_TAG="v0.65.0"
    KAGURA_WEB_IMAGE_REPO=""
    KAGURA_WEB_IMAGE_TAG=""

    run acquire_web_image
    [ "$status" -ne 0 ]
    [[ "$output" == *"web image"* ]]
}

@test "COMPOSE_FILE stays overridable for the split-host app tier" {
    run bash -c '
        COMPOSE_FILE=/tmp/app-tier.yml
        export COMPOSE_FILE
        source "'"$BATS_TEST_DIRNAME"'/../deploy.sh"
        echo "$COMPOSE_FILE"
    '
    [ "$status" -eq 0 ]
    [[ "$output" == *"/tmp/app-tier.yml"* ]]
}

# ---------------------------------------------------------------------------
# Registry-mode safety (review findings on #1513)
# ---------------------------------------------------------------------------
# The app services carry `build:` and no `image:`, so a tag miss makes compose
# build from source and still report success — shipping a HEAD build under a
# "registry" log line. And cmd_rollback restarts the previous color from local
# Docker state that nothing in this repo owns, which does not exist on a freshly
# provisioned app VM.

@test "registry mode passes --no-build so a tag miss fails loudly" {
    KAGURA_IMAGE_SOURCE="registry"
    dc_up_service "api-blue"
    run cat "$CALLS"
    [[ "$output" == *"dc up -d --no-deps --no-build api-blue"* ]]
}

@test "build mode does not pass --no-build" {
    dc_up_service "api-blue"
    run cat "$CALLS"
    [[ "$output" == *"dc up -d --no-deps api-blue"* ]]
    [[ "$output" != *"--no-build"* ]]
}

@test "extra flags survive the registry-mode wrapper" {
    KAGURA_IMAGE_SOURCE="registry"
    dc_up_service web --force-recreate
    run cat "$CALLS"
    [[ "$output" == *"--no-build --force-recreate web"* ]]
}

@test "rollback aborts when the previous color's image is gone in registry mode" {
    KAGURA_IMAGE_SOURCE="registry"
    KAGURA_IMAGE_REPO="registry.example.com/kagura-api"
    # Stub docker so `image inspect` reports the image as absent.
    mock_docker_missing() { [ "$1" = "image" ] && return 1; return 0; }
    export -f mock_docker_missing
    DOCKER=mock_docker_missing

    run ensure_rollback_image "green"
    [ "$status" -ne 0 ]
    [[ "$output" == *"Cannot roll back"* ]]
    # The remediation must name the exact tag compose will look for.
    [[ "$output" == *"single-server-api-green"* ]]
}

@test "rollback proceeds when the previous image is present" {
    KAGURA_IMAGE_SOURCE="registry"
    run ensure_rollback_image "green"   # mock_docker returns success for everything
    [ "$status" -eq 0 ]
}

@test "build mode rollback never consults the registry guard" {
    # No KAGURA_IMAGE_* configured at all: the guard must be a no-op, not an abort.
    run ensure_rollback_image "green"
    [ "$status" -eq 0 ]
}
