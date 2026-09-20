#!/usr/bin/env bats
# =============================================================================
# Static guard for the two log-hygiene properties of the single-server
# template (#1591):
#
#   1. ROTATION — every service a compose file defines carries the json-file
#      50m x 3 logging block. Docker's default json-file driver has no size
#      limit, and Caddy logs every request, so on a host that was not
#      provisioned by startup.sh (which writes a daemon.json) an access log
#      grows until the disk is full. The compose files must not depend on host
#      provisioning for this.
#   2. SCRUB — the site `log` block in Caddyfile.tpl wraps the json encoder in
#      a `filter` that redacts the closed-beta invite token (#1581) wherever
#      the proxy would record it, and the capture-group reference in the
#      replacement survives deploy.sh's envsubst render byte-for-byte.
#
# What the filter actually DOES to a log line is proven against a real Caddy in
# caddy_log_scrub_live.bats; this suite is the cheap half that needs no image
# and therefore always runs. The grep/awk assertions run everywhere; the
# `docker compose config` render (the form the issue's acceptance names) is
# added on top when compose is available, exactly as
# compose_tier_split_parity.bats does.
# =============================================================================

PROJECT_DIR="$BATS_TEST_DIRNAME/../.."
TPL="$PROJECT_DIR/Caddyfile.tpl"

# The four files that DEFINE services. docker-compose.data-expose.yml is a
# ports-only overlay: it patches services defined in data.yml and must inherit
# their logging through the compose merge, not repeat it.
COMPOSE_FILES=(
    docker-compose.app.yml
    docker-compose.prod.yml
    docker-compose.data.yml
    docker-compose.ollama.yml
)

# ---------------------------------------------------------------------------
# YAML helpers — awk only. The CI shell job installs bats and nothing else, so
# PyYAML cannot be assumed (same constraint as the parity suite).
# ---------------------------------------------------------------------------

# Print the body of a top-level key (`x-logging`, `x-api-common`, ...).
top_level_block() {   # $1 file, $2 key
    awk -v key="$2" '
        $0 ~ "^" key ":" { inside = 1; next }
        inside && /^[^[:space:]#]/ { exit }
        inside { print }
    ' "$1"
}

# Print "<service> <verdict>" for every entry under `services:`.
#   ok      — defines the service and carries `logging: *default-logging`,
#             directly or through `<<: *api-common`
#   MISSING — defines the service (image / build / api-common) without it
#   patch   — only patches a service defined in another file (no image, no
#             build, no api-common): exempt, the base definition carries it
service_logging_report() {   # $1 file
    local common_has_logging=0
    if top_level_block "$1" x-api-common | grep -qE '^  logging: \*default-logging[[:space:]]*$'; then
        common_has_logging=1
    fi
    awk -v common="$common_has_logging" '
        function flush() {
            if (name == "") return
            if (!defines)                                  verdict = "patch"
            else if (direct || (via_common && common))     verdict = "ok"
            else                                           verdict = "MISSING"
            print name, verdict
        }
        /^services:/ { in_services = 1; next }
        in_services && /^[^[:space:]#]/ { flush(); name = ""; in_services = 0 }
        !in_services { next }
        /^  [A-Za-z0-9_-]+:[[:space:]]*$/ {
            flush()
            name = $1; sub(/:$/, "", name)
            defines = 0; direct = 0; via_common = 0
            next
        }
        /^    (image|build):/                          { defines = 1 }
        /^    <<: \*api-common[[:space:]]*$/            { defines = 1; via_common = 1 }
        /^    logging: \*default-logging[[:space:]]*$/ { direct = 1 }
        END { flush() }
    ' "$1"
}

# The site `log { ... }` block of the template (tab-indented, one level deep).
log_block() {   # $1 file
    awk '/^\tlog \{/ { inside = 1 } inside { print } inside && /^\t\}/ { exit }' "$1"
}

# ---------------------------------------------------------------------------
# 1. Rotation
# ---------------------------------------------------------------------------

@test "rotation: each compose file anchors json-file / 50m / 3 as x-logging" {
    for f in "${COMPOSE_FILES[@]}"; do
        grep -qE '^x-logging: &default-logging[[:space:]]*$' "$PROJECT_DIR/$f" \
            || { echo "$f: no top-level 'x-logging: &default-logging'"; return 1; }
        block="$(top_level_block "$PROJECT_DIR/$f" x-logging)"
        [[ "$block" == *'driver: json-file'* ]] || { echo "$f: driver is not json-file"; return 1; }
        [[ "$block" == *'max-size: "50m"'* ]]   || { echo "$f: max-size is not \"50m\""; return 1; }
        [[ "$block" == *'max-file: "3"'* ]]     || { echo "$f: max-file is not \"3\""; return 1; }
    done
}

@test "rotation: every service a compose file defines carries the logging anchor" {
    for f in "${COMPOSE_FILES[@]}"; do
        run service_logging_report "$PROJECT_DIR/$f"
        [ "$status" -eq 0 ]
        if [[ "$output" == *MISSING* ]]; then
            echo "$f:"; echo "$output"
            return 1
        fi
    done
}

@test "rotation: the service census matches the files (guard not vacuous)" {
    # If a service is added or removed, update these counts deliberately — a
    # parser that silently sees zero services would make the test above pass.
    declare -A want=(
        [docker-compose.app.yml]=4
        [docker-compose.prod.yml]=7
        [docker-compose.data.yml]=3
        [docker-compose.ollama.yml]=1
    )
    for f in "${COMPOSE_FILES[@]}"; do
        n="$(service_logging_report "$PROJECT_DIR/$f" | grep -cv ' patch$' || true)"
        [ "$n" -eq "${want[$f]}" ] || { echo "$f: $n defined services, want ${want[$f]}"; return 1; }
    done
    # The two api-* entries of the ollama override only patch the base services.
    run service_logging_report "$PROJECT_DIR/docker-compose.ollama.yml"
    [[ "$output" == *"api-blue patch"* ]]
    [[ "$output" == *"api-green patch"* ]]
}

@test "rotation: the ports-only data overlay repeats no logging (it inherits through the merge)" {
    run grep -cE 'logging|x-logging' "$PROJECT_DIR/docker-compose.data-expose.yml"
    [ "$output" -eq 0 ]
}

@test "rotation: every documented composition renders 50m x 3 on every service" {
    docker compose version > /dev/null 2>&1 || skip "docker compose not available"

    work="$BATS_TEST_TMPDIR/render"   # bats removes it, also on a failed run
    mkdir -p "$work/single-server" "$work/backend" "$work/frontend"
    cp "$PROJECT_DIR"/docker-compose.*.yml "$work/single-server/"
    cat > "$work/single-server/.env.prod" <<'ENVEOF'
DB_PASSWORD=log-hygiene-test-pw
QDRANT_API_KEY=log-hygiene-test-key
KAGURA_DOMAIN=memory.example.com
NEXT_PUBLIC_ENABLE_GRAPH_VIZ=
NEXT_PUBLIC_PLAN_DISPLAY_NAMES=
ENVEOF

    # "<expected service count>|<compose -f arguments>" — the compositions the
    # README documents, plus the split pair the parity suite renders.
    compositions=(
        "4|-f docker-compose.app.yml"
        "3|-f docker-compose.data.yml"
        "7|-f docker-compose.prod.yml"
        "7|-f docker-compose.data.yml -f docker-compose.app.yml"
        "8|-f docker-compose.prod.yml -f docker-compose.ollama.yml"
        "3|-f docker-compose.data.yml -f docker-compose.data-expose.yml"
    )
    failed=0
    for entry in "${compositions[@]}"; do
        want_n="${entry%%|*}"
        files="${entry#*|}"
        # Scrubbed environment, as in the parity suite: an operator's exported
        # secrets must neither steer the render nor reach the test output.
        # shellcheck disable=SC2086  # $files is a deliberate word-split arg list
        if ! (cd "$work/single-server" \
                && env -u QDRANT_API_KEY -u DB_PASSWORD -u KAGURA_DOMAIN \
                       -u POSTGRES_HOST -u QDRANT_HOST -u REDIS_HOST \
                       -u COMPOSE_PROJECT_NAME -u COMPOSE_FILE \
                       DATA_BIND_ADDR=192.0.2.10 \
                    docker compose -p single-server $files --env-file .env.prod \
                        config --format json) > "$work/render.json" 2> "$work/render.err"; then
            echo "render failed: $files"; cat "$work/render.err"
            failed=1
            continue
        fi
        verdict="$(python3 - "$work/render.json" <<'PYEOF'
import json, sys

want = {"driver": "json-file", "options": {"max-size": "50m", "max-file": "3"}}
services = json.load(open(sys.argv[1]))["services"]
bad = sorted(name for name, svc in services.items() if svc.get("logging") != want)
print(f"{len(services)} {' '.join(bad) if bad else 'OK'}")
PYEOF
)"
        if [ "$verdict" != "$want_n OK" ]; then
            echo "$files -> '$verdict' (want '$want_n OK')"
            failed=1
        fi
    done
    [ "$failed" -eq 0 ]
}

# ---------------------------------------------------------------------------
# 2. Scrub
# ---------------------------------------------------------------------------

@test "scrub: the site log block wraps json in a filter encoder" {
    block="$(log_block "$TPL")"
    [ -n "$block" ]
    [[ "$block" == *"output stdout"* ]]
    [[ "$block" == *"format filter {"* ]]
    [[ "$block" == *"wrap json"* ]]
    # A bare `format json` is the pre-#1591 block that records the token.
    run grep -cE '^[[:space:]]*format json[[:space:]]*$' <<< "$block"
    [ "$output" -eq 0 ]
}

@test "scrub: request>uri is rewritten by ONE regexp covering the three invite URL shapes" {
    block="$(log_block "$TPL")"
    run grep -cE '^[[:space:]]*request>uri[[:space:]]' <<< "$block"
    [ "$output" -eq 1 ]
    line="$(grep -E '^[[:space:]]*request>uri[[:space:]]' <<< "$block")"
    [[ "$line" == *"request>uri regexp "* ]]
    [[ "$line" == *"(/join/)"* ]]
    [[ "$line" == *"(/beta-invites/)"*"(/preview)"* ]]
    [[ "$line" == *"([?&]invite=)"* ]]
    # Plain `regexp` only: multi_regexp needs a newer Caddy than a long-lived
    # host is guaranteed to run under the floating caddy:2-alpine tag.
    run grep -c 'multi_regexp' <<< "$block"
    [ "$output" -eq 0 ]
}

@test "scrub: the headers that carry the token (or session material) are dropped from the log" {
    block="$(log_block "$TPL")"
    for header in Referer Cookie Next-Router-State-Tree Next-Url; do
        grep -qE "^[[:space:]]*request>headers>${header} delete[[:space:]]*\$" <<< "$block" \
            || { echo "no 'request>headers>${header} delete'"; return 1; }
    done
}

@test "scrub: redirect response headers get the SAME regexp as the URI (no drift between copies)" {
    # The Caddyfile has no variables, so the pattern is written once per field.
    # Pin that the copies are identical — a fix applied to one and forgotten in
    # another would leave a field leaking.
    block="$(log_block "$TPL")"
    for field in 'resp_headers>Location' 'resp_headers>Refresh'; do
        grep -qE "^[[:space:]]*${field} regexp " <<< "$block" \
            || { echo "no '${field} regexp'"; return 1; }
    done
    run bash -o pipefail -c \
        'grep -E " regexp " | sed -E "s/^[[:space:]]*[^[:space:]]+ regexp //" | sort -u | wc -l' <<< "$block"
    [ "$status" -eq 0 ]
    [ "$output" -eq 1 ]
}

@test "scrub: the replacement holds no Caddy placeholder, only \${N} capture references" {
    block="$(log_block "$TPL")"
    # Second backtick-quoted token of the request>uri line = the replacement.
    replacement="$(grep -E '^[[:space:]]*request>uri regexp ' <<< "$block" | awk -F'`' '{ print $4 }')"
    [ -n "$replacement" ]
    [[ "$replacement" == *REDACTED* ]]
    # Strip the capture references; what is left must be brace-free, or Caddy
    # could read it as a {placeholder}.
    rest="$(sed -E 's/\$\{[0-9]+\}//g' <<< "$replacement")"
    [[ "$rest" != *"{"* ]]
    [[ "$rest" != *"}"* ]]
    [[ "$rest" != *'$'* ]]
}

@test "scrub: deploy.sh's envsubst render leaves the log block byte-identical" {
    command -v envsubst > /dev/null 2>&1 || skip "envsubst not available"

    # Drive the real generate_caddyfile (not a copy of its envsubst line) so
    # this keeps testing whatever allow-list deploy.sh actually uses. Only
    # ${API_UPSTREAM} may be expanded; `${1}`-style capture references in the
    # filter replacement must come out untouched.
    rendered="$BATS_TEST_TMPDIR/Caddyfile"
    run bash -c '
        set -euo pipefail
        source "'"$BATS_TEST_DIRNAME"'/../deploy.sh"
        CADDYFILE="'"$rendered"'"
        generate_caddyfile api-blue > /dev/null
    '
    [ "$status" -eq 0 ]
    [ -s "$rendered" ]

    diff <(log_block "$TPL") <(log_block "$rendered")
    # ...and the thing being guarded is really there (guard not vacuous).
    # shellcheck disable=SC2016  # literal ${1}: the regexp's capture reference
    log_block "$rendered" | grep -qF '${1}'

    # Whole-file view of the same property: the render may differ from the
    # template ONLY on lines that carried the ${API_UPSTREAM} placeholder.
    run bash -c 'diff "$1" "$2" | grep "^<" | grep -vcF "\${API_UPSTREAM}"' _ "$TPL" "$rendered"
    [ "$output" -eq 0 ]
    # shellcheck disable=SC2016  # the literal placeholder, not an expansion
    run grep -cF '${API_UPSTREAM}' "$rendered"
    [ "$output" -eq 0 ]
    grep -qF 'reverse_proxy api-blue:8080' "$rendered"
}

@test "scrub: the domain line, tls line and extension-point import are untouched" {
    # deploy.sh extracts the domain from the first line that starts with a
    # letter; a log-block edit must never disturb that, nor the top-level
    # import sibling vhosts rely on.
    run awk '/^[a-zA-Z]/ { gsub(/ *\{.*/, ""); print; exit }' "$TPL"
    [ "$status" -eq 0 ]
    [[ "$output" != *" "* ]]
    [[ "$output" == *.* ]]
    grep -qE '^	tls /etc/caddy/origin-ca/cert\.pem /etc/caddy/origin-ca/key\.pem$' "$TPL"
    grep -qE '^import /opt/kagura-caddy-extra/\*\.caddy$' "$TPL"
}
