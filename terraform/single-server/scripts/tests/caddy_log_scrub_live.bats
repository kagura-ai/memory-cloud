#!/usr/bin/env bats
# =============================================================================
# Behavioural proof for the access-log scrub in Caddyfile.tpl (#1591).
#
# A closed-beta invite link (#1581) is a credential that travels in URLs. The
# API scrubs it from its own logs, but the proxy in front records the request
# on its own — so the template's `log` block has to keep the token out of
# every field Caddy would write it to. Filter syntax is easy to get subtly
# wrong (a regexp that validates but never matches, a `${1}` eaten by a
# renderer), so this suite does not reason about the syntax: it renders the
# template exactly as deploy.sh does, boots it in the SAME image the compose
# files run, sends real requests and reads the JSON lines Caddy emits.
#
# Only two lines of the render are changed for the test — the site address
# (no public domain here) and the `tls` line (no origin certificate) — and a
# test pins that nothing else differs. The two upstreams are stood in for
# through the template's own extension point: a *.caddy file mounted at
# /opt/kagura-caddy-extra answers on :8080 / :3000, and `api-blue` / `web`
# are pointed at the container itself. The web stand-in reproduces the one
# upstream behaviour that matters here: a trailing-slash 308 whose `Location`
# and `Refresh` headers repeat the path, as the Next.js server does.
#
# Skips cleanly (never fails) without docker / curl / python3 / envsubst or
# when the image cannot be pulled. Publishes on a Docker-assigned free
# loopback port, removes its container and temp dir in teardown_file, and runs
# in a few seconds once the image is local.
# =============================================================================

PROJECT_DIR="$BATS_TEST_DIRNAME/../.."

# Token-shaped (URL-safe base64, 43 chars) and unique to this suite, so a
# plain grep over everything the container printed is a complete leak check.
TOKEN="k1591ScrubLiveTokenAbCdEfGhIjKlMnOpQrStUv_-9"

EXPECTED_CASES=11

setup_file() {
    export SCRUB_SKIP=""
    local tool
    for tool in docker curl python3 envsubst; do
        if ! command -v "$tool" > /dev/null 2>&1; then
            export SCRUB_SKIP="$tool not available"
            return 0
        fi
    done
    if ! docker info > /dev/null 2>&1; then
        export SCRUB_SKIP="docker daemon not reachable"
        return 0
    fi

    # Follow the image the stack actually runs instead of hard-coding it.
    IMAGE="$(awk '$1 == "image:" && $2 ~ /^caddy:/ { print $2; exit }' "$PROJECT_DIR/docker-compose.prod.yml")"
    export IMAGE
    if [ -z "$IMAGE" ]; then
        echo "no caddy image found in docker-compose.prod.yml" >&2
        return 1
    fi
    if ! docker image inspect "$IMAGE" > /dev/null 2>&1; then
        local pull=(docker pull -q "$IMAGE")
        if command -v timeout > /dev/null 2>&1; then pull=(timeout 120 "${pull[@]}"); fi
        if ! "${pull[@]}" > /dev/null 2>&1; then
            export SCRUB_SKIP="cannot pull $IMAGE"
            return 0
        fi
    fi

    WORK="$(mktemp -d)"
    export WORK
    mkdir -p "$WORK/extra"

    # 1. Render through the real generate_caddyfile, so the config under test
    #    has been through deploy.sh's envsubst allow-list.
    (
        set -euo pipefail
        # shellcheck disable=SC1091
        source "$BATS_TEST_DIRNAME/../deploy.sh"
        # shellcheck disable=SC2034  # read by generate_caddyfile
        CADDYFILE="$WORK/Caddyfile.rendered"
        generate_caddyfile api-blue
    ) > /dev/null

    # 2. Test-only neutralisation of the site address and the tls line.
    awk '
        !site && /^[a-zA-Z]/ && /\{[[:space:]]*$/ { print "http://:9080 {"; site = 1; next }
        /^\ttls \/etc\/caddy\/origin-ca\//         { next }
        { print }
    ' "$WORK/Caddyfile.rendered" > "$WORK/Caddyfile"

    # 3. Upstream stand-ins, delivered through the extension point. No `log`
    #    directive here: only the site under test writes access-log lines.
    cat > "$WORK/extra/upstreams.caddy" <<'CADDYEOF'
http://:8080 {
	respond "stub api" 200
}

http://:3000 {
	@slash path_regexp slash ^(.+)/$
	header @slash Refresh "0;url={re.slash.1}"
	redir @slash {re.slash.1} 308
	respond "stub web" 200
}
CADDYEOF

    NAME="k1591-caddy-scrub-$$-$RANDOM"
    export NAME
    docker run -d --name "$NAME" \
        -p 127.0.0.1::9080 \
        --add-host api-blue:127.0.0.1 --add-host web:127.0.0.1 \
        -v "$WORK/Caddyfile:/etc/caddy/Caddyfile:ro" \
        -v "$WORK/extra:/opt/kagura-caddy-extra:ro" \
        "$IMAGE" > /dev/null

    local port=""
    for _ in $(seq 1 40); do
        port="$(docker port "$NAME" 9080/tcp 2> /dev/null | head -n 1)"
        port="${port##*:}"
        if [ -n "$port" ] && curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$port/health"; then
            break
        fi
        sleep 0.5
    done
    export PORT="$port"

    # Every request carries the token in the four request headers a browser on
    # the /join/<token> page sends it in, so each line doubles as a header test.
    fire() {   # $1 case id, $2 path + query
        curl -s -o /dev/null --max-time 5 \
            -A "scrub-case-$1" \
            -H "Referer: http://memory.example.test/join/$TOKEN" \
            -H "Cookie: session=$TOKEN" \
            -H "Next-Url: /join/$TOKEN" \
            -H "Next-Router-State-Tree: %5B%22join%22%2C%5B%22token%22%2C%22$TOKEN%22%2C%22d%22%5D%5D" \
            "http://127.0.0.1:$PORT$2" || true
    }
    fire join          "/join/$TOKEN"
    fire join-rsc      "/join/$TOKEN?_rsc=1a2b3"
    fire join-pasted   "/join/$TOKEN%20"
    fire join-slash    "/join/$TOKEN/"
    fire preview       "/api/v1/beta-invites/$TOKEN/preview"
    fire login         "/api/v1/auth/google/login?return_to=x&invite=$TOKEN"
    fire login-first   "/api/v1/auth/github/login?invite=$TOKEN&return_to=x"
    fire plain-api     "/api/v1/memories?limit=5&q=join"
    fire plain-mcp     "/mcp"
    fire plain-root    "/"
    fire plain-invites "/api/v1/beta-invites/me"

    # The access-log line is written after the response completes; give the
    # last one a moment to land. stdout = access log (`output stdout`),
    # stderr = Caddy's own runtime log.
    for _ in $(seq 1 20); do
        [ "$(docker logs "$NAME" 2> /dev/null | grep -c '"scrub-case-')" -ge "$EXPECTED_CASES" ] && break
        sleep 0.25
    done
    docker logs "$NAME" > "$WORK/access.log" 2> "$WORK/runtime.log"
}

teardown_file() {
    [ -n "${NAME:-}" ] && docker rm -f "$NAME" > /dev/null 2>&1
    [ -n "${WORK:-}" ] && rm -rf "$WORK"
    return 0
}

require_live() {
    [ -z "${SCRUB_SKIP:-}" ] || skip "$SCRUB_SKIP"
}

# Print one value from the access-log line of a case:
#   uri | status | req_headers (sorted names) | resp:<Header>
logged() {   # $1 case id, $2 what
    python3 - "$WORK/access.log" "scrub-case-$1" "$2" <<'PYEOF'
import json, sys

path, agent, what = sys.argv[1:4]
lines = [json.loads(raw) for raw in open(path) if raw.strip()]
hits = [d for d in lines if d["request"]["headers"].get("User-Agent") == [agent]]
if len(hits) != 1:
    sys.exit(f"{agent}: {len(hits)} access-log lines, want exactly 1")
d = hits[0]
if what == "uri":
    print(d["request"]["uri"])
elif what == "status":
    print(d["status"])
elif what == "req_headers":
    print(" ".join(sorted(d["request"]["headers"])))
elif what.startswith("resp:"):
    print(" ".join(d.get("resp_headers", {}).get(what[5:], [])))
else:
    sys.exit(f"unknown selector {what}")
PYEOF
}

# Equality with a readable failure (bats prints only the failing line).
expect() {   # $1 actual, $2 wanted
    [ "$1" = "$2" ] || { echo "got:  $1"; echo "want: $2"; return 1; }
}

# ---------------------------------------------------------------------------
# The config under test is the real one
# ---------------------------------------------------------------------------

@test "live: the test config is deploy.sh's render minus the site address and the tls line" {
    require_live
    # 2 lines out ("<": domain, tls), 1 line in (">": the :9080 address).
    run bash -c 'diff "$1" "$2" | grep -c "^[<>]"' _ "$WORK/Caddyfile.rendered" "$WORK/Caddyfile"
    [ "$output" -eq 3 ]
    run bash -c 'diff "$1" "$2" | grep "^>"' _ "$WORK/Caddyfile.rendered" "$WORK/Caddyfile"
    [ "$output" = "> http://:9080 {" ]
}

@test "live: the rendered template validates in the image the stack runs" {
    require_live
    run docker run --rm \
        -v "$WORK/Caddyfile:/etc/caddy/Caddyfile:ro" \
        -v "$WORK/extra:/opt/kagura-caddy-extra:ro" \
        "$IMAGE" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
    [ "$status" -eq 0 ] || { echo "$output"; return 1; }
    [[ "$output" == *"Valid configuration"* ]]
}

@test "live: the capture references reach Caddy's JSON config intact (not read as placeholders)" {
    require_live
    docker run --rm \
        -v "$WORK/Caddyfile:/etc/caddy/Caddyfile:ro" \
        -v "$WORK/extra:/opt/kagura-caddy-extra:ro" \
        "$IMAGE" caddy adapt --config /etc/caddy/Caddyfile --adapter caddyfile \
        > "$WORK/adapted.json" 2> /dev/null
    run python3 - "$WORK/adapted.json" <<'PYEOF'
import json, sys

logs = json.load(open(sys.argv[1]))["logging"]["logs"]
fields = next(log["encoder"]["fields"] for log in logs.values()
              if log.get("encoder", {}).get("format") == "filter")
uri = fields["request>uri"]
print(uri["filter"], uri["value"])
PYEOF
    [ "$status" -eq 0 ] || { echo "$output"; return 1; }
    # shellcheck disable=SC2016  # literal ${N}: the regexp's capture references
    [ "$output" = 'regexp ${1}${2}${4}REDACTED${3}' ]
}

@test "live: the stack came up and logged every request once (guard not vacuous)" {
    require_live
    [ -n "$PORT" ]
    run grep -c '"scrub-case-' "$WORK/access.log"
    [ "$output" -eq "$EXPECTED_CASES" ]
    # Requests really went THROUGH the proxy to the stand-in upstreams: a 502
    # here would mean the log lines under test are not the production path.
    run logged join status
    expect "$output" "200"
    run logged preview status
    expect "$output" "200"
}

# ---------------------------------------------------------------------------
# The token is redacted, the line stays readable
# ---------------------------------------------------------------------------

@test "live: GET /join/<token> logs the token slot as REDACTED" {
    require_live
    run logged join uri
    expect "$output" "/join/REDACTED"
    run logged join-rsc uri
    expect "$output" "/join/REDACTED?_rsc=1a2b3"
}

@test "live: a pasted link with a trailing %20 is still redacted (fail-safe width)" {
    require_live
    run logged join-pasted uri
    expect "$output" "/join/REDACTED"
}

@test "live: GET /api/v1/beta-invites/<token>/preview keeps the route, drops the token" {
    require_live
    run logged preview uri
    expect "$output" "/api/v1/beta-invites/REDACTED/preview"
}

@test "live: invite=<token> is redacted wherever it sits in the query" {
    require_live
    run logged login uri
    expect "$output" "/api/v1/auth/google/login?return_to=x&invite=REDACTED"
    run logged login-first uri
    expect "$output" "/api/v1/auth/github/login?invite=REDACTED&return_to=x"
}

@test "live: a trailing-slash 308 does not leak the token through Location / Refresh" {
    require_live
    run logged join-slash status
    expect "$output" "308"
    run logged join-slash uri
    expect "$output" "/join/REDACTED/"
    run logged join-slash resp:Location
    expect "$output" "/join/REDACTED"
    run logged join-slash resp:Refresh
    expect "$output" "0;url=/join/REDACTED"
}

@test "live: no Referer / Cookie / Next-Router-State-Tree / Next-Url key in any line" {
    require_live
    for c in join join-rsc join-pasted join-slash preview login login-first \
             plain-api plain-mcp plain-root plain-invites; do
        run logged "$c" req_headers
        [ "$status" -eq 0 ] || { echo "$output"; return 1; }
        # What is left is exactly what curl sent besides the four.
        [ "$output" = "Accept User-Agent" ] || { echo "$c: $output"; return 1; }
    done
}

@test "live: the token appears NOWHERE in what the container printed (stdout + stderr)" {
    require_live
    run grep -c "$TOKEN" "$WORK/access.log" "$WORK/runtime.log"
    [[ "$output" == *"access.log:0"* ]]
    [[ "$output" == *"runtime.log:0"* ]]
}

@test "live: ordinary requests keep their full URI" {
    require_live
    run logged plain-api uri
    expect "$output" "/api/v1/memories?limit=5&q=join"
    run logged plain-mcp uri
    expect "$output" "/mcp"
    run logged plain-root uri
    expect "$output" "/"
    # Same route family as the preview, but no token slot: left alone.
    run logged plain-invites uri
    expect "$output" "/api/v1/beta-invites/me"
}
