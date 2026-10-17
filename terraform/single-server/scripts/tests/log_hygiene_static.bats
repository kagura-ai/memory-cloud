#!/usr/bin/env bats
# =============================================================================
# Static guard for the two log-hygiene properties of the single-server
# template (#1591):
#
#   1. ROTATION — every service a compose file defines carries the json-file
#      logging block, 50m x 3 unless the operator overrides it (#2025:
#      LOG_MAX_SIZE / LOG_MAX_FILE, and API_LOG_MAX_SIZE / API_LOG_MAX_FILE for
#      the API colors). Docker's default json-file driver has no size
#      limit, and Caddy logs every request, so on a host that was not
#      provisioned by startup.sh (which writes a daemon.json) an access log
#      grows until the disk is full. The compose files must not depend on host
#      provisioning for this.
#   2. SCRUB — BOTH `log` blocks in Caddyfile.tpl wrap the json encoder in a
#      `filter` that redacts the closed-beta invite token (#1581) wherever the
#      proxy would record it: the site's access logger, and the default logger
#      in the global options, which carries the http.log.error.* line Caddy
#      writes about a request whose upstream failed. The capture-group
#      reference in the replacement survives deploy.sh's envsubst render
#      byte-for-byte.
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
#   ok      — defines the service and carries `logging: *default-logging`
#             directly, or `logging: *api-logging` through `<<: *api-common`
#   MISSING — defines the service (image / build / api-common) without it
#   patch   — only patches a service defined in another file (no image, no
#             build, no api-common): exempt, the base definition carries it
service_logging_report() {   # $1 file
    local common_has_logging=0
    if top_level_block "$1" x-api-common | grep -qE '^  logging: \*api-logging[[:space:]]*$'; then
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

# The site `log { ... }` block of the template (tab-indented, one level deep):
# the ACCESS logger.
log_block() {   # $1 file
    awk '/^\tlog \{/ { inside = 1 } inside { print } inside && /^\t\}/ { exit }' "$1"
}

# The `log default { ... }` block of the global options (same indentation): the
# DEFAULT logger, which writes the error line of a request whose upstream
# failed.
default_log_block() {   # $1 file
    awk '/^\tlog default \{/ { inside = 1 } inside { print } inside && /^\t\}/ { exit }' "$1"
}

# ---------------------------------------------------------------------------
# 1. Rotation
# ---------------------------------------------------------------------------

@test "rotation: each compose file anchors json-file with the LOG_MAX_* knobs (default 50m / 3) as x-logging" {
    for f in "${COMPOSE_FILES[@]}"; do
        grep -qE '^x-logging: &default-logging[[:space:]]*$' "$PROJECT_DIR/$f" \
            || { echo "$f: no top-level 'x-logging: &default-logging'"; return 1; }
        block="$(top_level_block "$PROJECT_DIR/$f" x-logging)"
        [[ "$block" == *'driver: json-file'* ]] || { echo "$f: driver is not json-file"; return 1; }
        # Quoted, so the default renders as the same string as before (#2025).
        # shellcheck disable=SC2016  # literal ${VAR:-default}: the file text
        [[ "$block" == *'max-size: "${LOG_MAX_SIZE:-50m}"'* ]] \
            || { echo "$f: max-size is not the quoted LOG_MAX_SIZE form"; return 1; }
        # shellcheck disable=SC2016
        [[ "$block" == *'max-file: "${LOG_MAX_FILE:-3}"'* ]] \
            || { echo "$f: max-file is not the quoted LOG_MAX_FILE form"; return 1; }
    done
}

@test "rotation: the files that define the API colors anchor their own x-api-logging (#2025)" {
    for f in docker-compose.app.yml docker-compose.prod.yml; do
        grep -qE '^x-api-logging: &api-logging[[:space:]]*$' "$PROJECT_DIR/$f" \
            || { echo "$f: no top-level 'x-api-logging: &api-logging'"; return 1; }
        block="$(top_level_block "$PROJECT_DIR/$f" x-api-logging)"
        [[ "$block" == *'driver: json-file'* ]] || { echo "$f: driver is not json-file"; return 1; }
        # shellcheck disable=SC2016  # literal ${VAR:-default}: the file text
        [[ "$block" == *'max-size: "${API_LOG_MAX_SIZE:-50m}"'* ]] \
            || { echo "$f: max-size is not the quoted API_LOG_MAX_SIZE form"; return 1; }
        # shellcheck disable=SC2016
        [[ "$block" == *'max-file: "${API_LOG_MAX_FILE:-3}"'* ]] \
            || { echo "$f: max-file is not the quoted API_LOG_MAX_FILE form"; return 1; }
        # api-blue / api-green get it through x-api-common, and nothing else does.
        top_level_block "$PROJECT_DIR/$f" x-api-common | grep -qE '^  logging: \*api-logging[[:space:]]*$' \
            || { echo "$f: x-api-common does not carry 'logging: *api-logging'"; return 1; }
        run grep -cE '^    logging: \*api-logging' "$PROJECT_DIR/$f"
        [ "$output" -eq 0 ] || { echo "$f: a non-API service uses *api-logging"; return 1; }
    done
    # The other files define no API color (ollama.yml only patches them).
    for f in docker-compose.data.yml docker-compose.ollama.yml; do
        run grep -c 'api-logging' "$PROJECT_DIR/$f"
        [ "$output" -eq 0 ] || { echo "$f: unexpected api-logging"; return 1; }
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

# Copy the compose files into a scratch tree with a test .env.prod; extra
# arguments are appended to it as KEY=value lines. Prints the work dir.
render_tree() {   # [KEY=value ...]
    local work="$BATS_TEST_TMPDIR/render"   # bats removes it, also on a failed run
    mkdir -p "$work/single-server" "$work/backend" "$work/frontend"
    cp "$PROJECT_DIR"/docker-compose.*.yml "$work/single-server/"
    cat > "$work/single-server/.env.prod" <<'ENVEOF'
DB_PASSWORD=log-hygiene-test-pw
QDRANT_API_KEY=log-hygiene-test-key
KAGURA_DOMAIN=memory.example.com
NEXT_PUBLIC_ENABLE_GRAPH_VIZ=
NEXT_PUBLIC_PLAN_DISPLAY_NAMES=
ENVEOF
    [ "$#" -eq 0 ] || printf '%s\n' "$@" >> "$work/single-server/.env.prod"
    echo "$work"
}

# `docker compose config --format json` of one composition, read with
# --env-file .env.prod as deploy.sh does. Scrubbed environment, as in the
# parity suite: an operator's exported secrets — or rotation knobs — must
# neither steer the render nor reach the test output.
render_composition() {   # $1 work dir, $2 compose -f arguments
    # shellcheck disable=SC2086  # $2 is a deliberate word-split arg list
    (cd "$1/single-server" \
        && env -u QDRANT_API_KEY -u DB_PASSWORD -u KAGURA_DOMAIN \
               -u POSTGRES_HOST -u QDRANT_HOST -u REDIS_HOST \
               -u REDIS_PASSWORD -u REDIS_URL \
               -u LOG_MAX_SIZE -u LOG_MAX_FILE -u API_LOG_MAX_SIZE -u API_LOG_MAX_FILE \
               -u COMPOSE_PROJECT_NAME -u COMPOSE_FILE \
               DATA_BIND_ADDR=192.0.2.10 \
            docker compose -p single-server $2 --env-file .env.prod \
                config --format json)
}

# "<service count> <services whose logging is off, or OK>" for a rendered
# composition: api-* services are held to $2 / $3, every other one to $4 / $5.
logging_verdict() {   # $1 render.json, $2 api max-size, $3 api max-file, $4 max-size, $5 max-file
    python3 - "$@" <<'PYEOF'
import json, sys

path, api_size, api_files, size, files = sys.argv[1:6]


def want(max_size, max_file):
    return {"driver": "json-file", "options": {"max-size": max_size, "max-file": max_file}}


services = json.load(open(path))["services"]
bad = sorted(
    name
    for name, svc in services.items()
    if svc.get("logging")
    != (want(api_size, api_files) if name.startswith("api-") else want(size, files))
)
print(f"{len(services)} {' '.join(bad) if bad else 'OK'}")
PYEOF
}

@test "rotation: every documented composition renders 50m x 3 on every service" {
    docker compose version > /dev/null 2>&1 || skip "docker compose not available"

    work="$(render_tree)"

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
        if ! render_composition "$work" "$files" > "$work/render.json" 2> "$work/render.err"; then
            echo "render failed: $files"; cat "$work/render.err"
            failed=1
            continue
        fi
        # Unset knobs render exactly the pre-#2025 block, as strings, everywhere.
        verdict="$(logging_verdict "$work/render.json" 50m 3 50m 3)"
        if [ "$verdict" != "$want_n OK" ]; then
            echo "$files -> '$verdict' (want '$want_n OK')"
            failed=1
        fi
    done
    [ "$failed" -eq 0 ]
}

@test "rotation: API_LOG_* and LOG_* overrides in .env.prod render independently (#2025)" {
    docker compose version > /dev/null 2>&1 || skip "docker compose not available"

    # "<expected service count>|<compose -f arguments>" — every composition
    # that runs the API colors next to other services.
    compositions=(
        "4|-f docker-compose.app.yml"
        "7|-f docker-compose.prod.yml"
        "7|-f docker-compose.data.yml -f docker-compose.app.yml"
        "8|-f docker-compose.prod.yml -f docker-compose.ollama.yml"
    )
    # "<.env.prod lines>|<api max-size> <api max-file> <max-size> <max-file>":
    # each pair of knobs moves its own services and leaves the others at 50m x 3.
    cases=(
        "API_LOG_MAX_SIZE=200m API_LOG_MAX_FILE=7|200m 7 50m 3"
        "LOG_MAX_SIZE=20m LOG_MAX_FILE=5|50m 3 20m 5"
    )
    failed=0
    for case in "${cases[@]}"; do
        read -r -a env_lines <<< "${case%%|*}"
        read -r -a want <<< "${case#*|}"
        work="$(render_tree "${env_lines[@]}")"
        for entry in "${compositions[@]}"; do
            want_n="${entry%%|*}"
            files="${entry#*|}"
            if ! render_composition "$work" "$files" > "$work/render.json" 2> "$work/render.err"; then
                echo "render failed: $files (${case%%|*})"; cat "$work/render.err"
                failed=1
                continue
            fi
            verdict="$(logging_verdict "$work/render.json" "${want[@]}")"
            if [ "$verdict" != "$want_n OK" ]; then
                echo "$files with ${case%%|*} -> '$verdict' (want '$want_n OK')"
                failed=1
            fi
        done
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

@test "scrub: the default logger (upstream-error lines) gets a filter encoder too" {
    block="$(default_log_block "$TPL")"
    [ -n "$block" ]
    [[ "$block" == *"format filter {"* ]]
    [[ "$block" == *"wrap json"* ]]
    # It is a GLOBAL option: it has to sit in the options block, above the
    # first site address (the line deploy.sh reads the domain from).
    log_at="$(grep -nE '^	log default \{' "$TPL" | cut -d: -f1)"
    site_at="$(grep -nE '^[a-zA-Z]' "$TPL" | head -n 1 | cut -d: -f1)"
    [ "$log_at" -lt "$site_at" ]
}

@test "scrub: request>uri is rewritten by ONE regexp covering the six invite URL shapes" {
    for block in "$(log_block "$TPL")" "$(default_log_block "$TPL")"; do
        run grep -cE '^[[:space:]]*request>uri[[:space:]]' <<< "$block"
        [ "$output" -eq 1 ]
        line="$(grep -E '^[[:space:]]*request>uri[[:space:]]' <<< "$block")"
        [[ "$line" == *"request>uri regexp "* ]]
        # Closed-beta invite links (#1581).
        [[ "$line" == *"(/join/)"* ]]
        [[ "$line" == *"(/beta-invites/)"*"(/preview)"* ]]
        [[ "$line" == *"([?&]invite=)"* ]]
        # Workspace invitation links: the landing page, the preview API call,
        # and the landing URL percent-encoded in the OAuth login's return_to.
        [[ "$line" == *"(/invite/)"* ]]
        [[ "$line" == *"(/v1/invitations/)"* ]]
        [[ "$line" == *"(%2[Ff]invite%2[Ff])"* ]]
        # Plain `regexp` only: multi_regexp needs a newer Caddy than a
        # long-lived host is guaranteed to run under the floating
        # caddy:2-alpine tag.
        run grep -c 'multi_regexp' <<< "$block"
        [ "$output" -eq 0 ]
    done
}

@test "scrub: the headers that carry the token (or session material) are dropped from both logs" {
    for block in "$(log_block "$TPL")" "$(default_log_block "$TPL")"; do
        for header in Referer Cookie Next-Router-State-Tree Next-Url X-Resource-Api-Key; do
            grep -qE "^[[:space:]]*request>headers>${header} delete[[:space:]]*\$" <<< "$block" \
                || { echo "no 'request>headers>${header} delete' in: ${block%%$'\n'*}"; return 1; }
        done
    done
    # No drift: a header added to one block and forgotten in the other would
    # leave it in the other logger's lines.
    diff <(log_block "$TPL" | grep -E ' delete[[:space:]]*$' | sed -E 's/^[[:space:]]+//' | sort) \
         <(default_log_block "$TPL" | grep -E ' delete[[:space:]]*$' | sed -E 's/^[[:space:]]+//' | sort)
}

@test "scrub: the resource-token header is deleted under Go's canonical name in both logs" {
    # Resource ingestion authenticates with a bearer-equivalent secret in the
    # X-Resource-API-Key request header. Go canonicalises header names, so the
    # key Caddy writes is X-Resource-Api-Key, and the filter looks keys up
    # case-sensitively: a delete spelled X-Resource-API-Key (the API's own
    # spelling) validates fine and matches nothing. Pin the exact spelling in
    # both blocks; caddy_log_scrub_live.bats proves it against the real image.
    for block in "$(log_block "$TPL")" "$(default_log_block "$TPL")"; do
        run grep -ciE '^[[:space:]]*request>headers>X-Resource-API-Key delete[[:space:]]*$' <<< "$block"
        [ "$output" -eq 1 ] || { echo "want exactly one resource-token delete, got $output in: ${block%%$'\n'*}"; return 1; }
        run grep -cE '^[[:space:]]*request>headers>X-Resource-Api-Key delete[[:space:]]*$' <<< "$block"
        [ "$output" -eq 1 ] || { echo "resource-token delete is not spelled X-Resource-Api-Key in: ${block%%$'\n'*}"; return 1; }
    done
}

@test "scrub: every copy of the regexp is the SAME — redirect headers and the default logger (no drift)" {
    # The Caddyfile has no variables, so the pattern is written once per field
    # and once more for the default logger. Pin that the copies are identical
    # — a fix applied to one and forgotten in another would leave a field, or
    # a whole logger, leaking.
    block="$(log_block "$TPL")"
    for field in 'resp_headers>Location' 'resp_headers>Refresh'; do
        grep -qE "^[[:space:]]*${field} regexp " <<< "$block" \
            || { echo "no '${field} regexp'"; return 1; }
    done
    both="$block"$'\n'"$(default_log_block "$TPL")"
    # Four copies (guard not vacuous) ...
    run grep -cE ' regexp ' <<< "$both"
    [ "$output" -eq 4 ]
    # ... one distinct "<pattern> <replacement>".
    run bash -o pipefail -c \
        'grep -E " regexp " | sed -E "s/^[[:space:]]*[^[:space:]]+ regexp //" | sort -u | wc -l' <<< "$both"
    [ "$status" -eq 0 ]
    [ "$output" -eq 1 ]
}

@test "scrub: the replacement holds no Caddy placeholder, only \${N} capture references" {
    for block in "$(log_block "$TPL")" "$(default_log_block "$TPL")"; do
        # Second backtick-quoted token of the request>uri line = the replacement.
        replacement="$(grep -E '^[[:space:]]*request>uri regexp ' <<< "$block" | awk -F'`' '{ print $4 }')"
        [ -n "$replacement" ]
        [[ "$replacement" == *REDACTED* ]]
        # Strip the capture references; what is left must be brace-free, or
        # Caddy could read it as a {placeholder}.
        rest="$(sed -E 's/\$\{[0-9]+\}//g' <<< "$replacement")"
        [[ "$rest" != *"{"* ]]
        [[ "$rest" != *"}"* ]]
        [[ "$rest" != *'$'* ]]
    done
}

@test "scrub: deploy.sh's envsubst render leaves both log blocks byte-identical" {
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
    diff <(default_log_block "$TPL") <(default_log_block "$rendered")
    # ...and the thing being guarded is really there (guard not vacuous).
    # shellcheck disable=SC2016  # literal ${1}: the regexp's capture reference
    log_block "$rendered" | grep -qF '${1}'
    # shellcheck disable=SC2016  # literal ${1}: the regexp's capture reference
    default_log_block "$rendered" | grep -qF '${1}'

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
