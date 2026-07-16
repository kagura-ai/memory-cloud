#!/usr/bin/env bats
# =============================================================================
# Regression guard for the #1310 pin-divergence hazard: the postgres image is
# digest-pinned as a LITERAL in six sites across four files (the ci.yml YAML
# anchor was inlined so Renovate's github-actions manager detects every site).
# YAML no longer enforces that the literals agree — this test does. The
# Renovate packageRule group keeps them moving together once the bot is
# active; this guard catches a stray hand-edit of a single site in the
# meantime and forever. Static conformance — no docker, no network.
# =============================================================================

REPO_ROOT="$BATS_TEST_DIRNAME/../../../.."

PIN_FILES=(
    "docker-compose.yml"
    "terraform/single-server/docker-compose.prod.yml"
    ".github/workflows/ci.yml"
    ".github/workflows/eval-nightly.yml"
)

@test "postgres image pin is identical across every site" {
    cd "$REPO_ROOT"
    for f in "${PIN_FILES[@]}"; do [ -r "$f" ]; done
    run bash -o pipefail -c \
        'grep -hoE "postgres:[0-9][^\"[:space:]]*@sha256:[a-f0-9]{64}" "$@" | sort -u | wc -l' \
        _ "${PIN_FILES[@]}"
    [ "$status" -eq 0 ]
    [ "$output" -eq 1 ]
}

@test "all six pin sites are present (guard not vacuous)" {
    # 2 compose files + 3 ci.yml service blocks + eval-nightly. If a service
    # block is added or removed, update this floor deliberately.
    cd "$REPO_ROOT"
    run bash -o pipefail -c \
        'grep -hoE "postgres:[0-9][^\"[:space:]]*@sha256:[a-f0-9]{64}" "$@" | wc -l' \
        _ "${PIN_FILES[@]}"
    [ "$status" -eq 0 ]
    [ "$output" -ge 6 ]
}
