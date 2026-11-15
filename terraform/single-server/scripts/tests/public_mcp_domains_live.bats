#!/usr/bin/env bats
# =============================================================================
# Behavioural proof for the optional custom-domain site of Caddyfile.tpl
# (#2809): custom domains of public MCP endpoints, served by on-demand TLS.
#
# The template ships the site commented out behind a line marker. This suite
# enables it exactly as an operator does (the anchored sed of README.md
# "Custom domains (optional)"), renders it through deploy.sh's real
# generate_caddyfile, boots it in the SAME image the compose file runs and
# sends real TLS requests. What it proves:
#
#   1. the enabled render passes `caddy validate`;
#   2. an allowed name gets a certificate (the proxy asks the permission path
#      with ?domain=<name> itself) and /mcp reaches the upstream with the
#      client's Host, without the client's Cookie / Authorization, and no
#      Set-Cookie of the upstream comes back;
#   3. every other path on that name is 404 and reaches no upstream;
#   4. a name the permission path refuses gets no certificate;
#   5. a request whose Host differs from the TLS server name (localhost, an
#      address literal, the main site's name with a trailing dot, an
#      HTTP/1.0 request without Host) is 404 and reaches no upstream;
#   6. the site without the global permission lines obtains no certificate:
#      the configuration does not load, or the handshake fails (the test
#      accepts either and prints which).
#
# Test-only changes to the render, each pinned by a test below: the same two
# lines as caddy_log_scrub_live.bats (the main site address becomes
# `http://:9080 {`, the origin-certificate `tls` line is dropped, as no public
# name or origin certificate exists here), and, for the requests of cases 2-5
# only, `issuer acme { ... }` becomes `issuer internal` so that a certificate
# can be issued without a CA. Cases 1 and 6 keep the issuer as shipped. The
# upstream is stood in for through the template's own extension point: a
# *.caddy file mounted at /opt/kagura-caddy-extra answers on :8080, and
# `api-blue` points at the container itself. The stand-in writes an access
# log, so "reaches no upstream" is read from what it recorded.
#
# Skips cleanly (never fails) without docker / curl / python3 / envsubst or
# when the image cannot be pulled. Publishes on a Docker-assigned free
# loopback port, removes its containers and temp dir in teardown_file.
# =============================================================================

PROJECT_DIR="$BATS_TEST_DIRNAME/../.."
TPL="$PROJECT_DIR/Caddyfile.tpl"

MARKER_SED='s/^#public-mcp-domains# //'
PERMISSION_PATH="/internal/public-mcp-domains/tls-permission"
ALLOWED="allowed.example.org"
DENIED="denied.example.org"

# Unique to this suite, so a plain grep over what the stand-in saw is a
# complete check that the values did not travel upstream.
COOKIE_VALUE="kmc2809LiveCookieValue"
AUTH_VALUE="Bearer kmc2809LiveAuthorizationValue"

# Every container this suite starts carries this prefix.
NAME_PREFIX="kmc-2809proxy-"

setup_file() {
    export LIVE_SKIP=""
    local tool
    for tool in docker curl python3 envsubst; do
        if ! command -v "$tool" > /dev/null 2>&1; then
            export LIVE_SKIP="$tool not available"
            return 0
        fi
    done
    if ! docker info > /dev/null 2>&1; then
        export LIVE_SKIP="docker daemon not reachable"
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
            export LIVE_SKIP="cannot pull $IMAGE"
            return 0
        fi
    fi

    WORK="$(mktemp -d)"
    export WORK
    mkdir -p "$WORK/extra"
    export SUFFIX="$$-$RANDOM"
    # The version under test, printed with case 6.
    IMAGE_VERSION="$(docker run --rm --name "${NAME_PREFIX}version-$SUFFIX" "$IMAGE" caddy version 2> /dev/null | awk '{ print $1; exit }' || true)"
    export IMAGE_VERSION
    export NAME="${NAME_PREFIX}live-$SUFFIX"
    export NAME6="${NAME_PREFIX}nopermission-$SUFFIX"

    # The main site's address, read with deploy.sh's rule (first letter-led
    # line), for the trailing-dot Host case.
    MAIN_SITE="$(awk '/^[a-zA-Z]/ { gsub(/ *\{.*/, ""); print; exit }' "$TPL")"
    export MAIN_SITE

    # 1. Two template copies: everything enabled (the operator's sed), and
    #    only the site enabled (the global on_demand_tls lines left commented).
    sed "$MARKER_SED" "$TPL" > "$WORK/enabled.tpl"
    local site_at
    site_at="$(grep -nE '^[a-zA-Z]' "$TPL" | head -n 1 | cut -d: -f1)"
    awk -v site="$site_at" 'NR > site { sub(/^#public-mcp-domains# /, "") } { print }' "$TPL" > "$WORK/site-only.tpl"

    # 2. Render each through the real generate_caddyfile.
    local tpl
    for tpl in enabled site-only; do
        (
            set -euo pipefail
            export CADDYFILE_TPL="$WORK/$tpl.tpl"
            # shellcheck disable=SC1091
            source "$BATS_TEST_DIRNAME/../deploy.sh"
            # shellcheck disable=SC2034  # read by generate_caddyfile
            CADDYFILE="$WORK/$tpl.rendered"
            generate_caddyfile api-blue
        ) > /dev/null
        # 3. The scrub suite's two-line neutralisation, nothing else...
        awk '
            !site && /^[a-zA-Z]/ && /\{[[:space:]]*$/ { print "http://:9080 {"; site = 1; next }
            /^\ttls \/etc\/caddy\/origin-ca\//         { next }
            { print }
        ' "$WORK/$tpl.rendered" > "$WORK/$tpl.acme"
        # 4. ...and the issuer, for the requests (no CA reachable here).
        awk '
            /^\t\tissuer acme \{$/ { print "\t\tissuer internal"; skip = 1; next }
            skip && /^\t\t\}$/     { skip = 0; next }
            skip                   { next }
            { print }
        ' "$WORK/$tpl.acme" > "$WORK/$tpl.internal"
    done

    # 5. The upstream stand-in, delivered through the extension point. The
    #    permission path answers 200 only for the allowed name; /mcp echoes
    #    what arrived and sets a cookie; everything else is a 404 that names
    #    the stand-in. Its access log (stdout) records every request it saw.
    cat > "$WORK/extra/standin.caddy" <<CADDYEOF
http://:8080 {
	log {
		output stdout
		format json
	}
	@allowed {
		path $PERMISSION_PATH
		query domain=$ALLOWED
	}
	@mcp path /mcp /mcp/
	route {
		respond @allowed 200
		respond $PERMISSION_PATH 404
		header @mcp Set-Cookie "standin=kmc2809; Path=/"
		respond @mcp "standin host={http.request.host} cookie=[{http.request.header.Cookie}] authorization=[{http.request.header.Authorization}]" 200
		respond "standin other" 404
	}
}
CADDYEOF

    # 6. The site without its permission lines, with the issuer as shipped
    #    (acme): does the configuration load? Not the internal issuer here:
    #    Caddy lets on-demand issuance from its internal CA run without a
    #    permission module (measured on 2.11.7), so that variant would prove
    #    nothing about the shipped site.
    local rc6=0
    docker run --rm --name "${NAME6}-validate" \
        -v "$WORK/site-only.acme:/etc/caddy/Caddyfile:ro" \
        -v "$WORK/extra:/opt/kagura-caddy-extra:ro" \
        "$IMAGE" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile \
        > "$WORK/case6.validate" 2>&1 || rc6=$?
    echo "$rc6" > "$WORK/case6.validate.rc"
    if [ "$rc6" -eq 0 ]; then
        # It loads: then no handshake may succeed. The CA hosts point at the
        # container itself, so no order leaves this machine.
        start_proxy "$NAME6" "$WORK/site-only.acme" \
            --add-host acme-v02.api.letsencrypt.org:127.0.0.1 \
            --add-host acme.zerossl.com:127.0.0.1
        local port6
        port6="$(wait_for_proxy "$NAME6")"
        fire_on "$port6" case6 "$ALLOWED" /mcp
    fi

    # The proxy under test: everything enabled, internal issuer.
    start_proxy "$NAME" "$WORK/enabled.internal"
    PORT="$(wait_for_proxy "$NAME")"
    export PORT

    # Case 2: the allowed name, /mcp and /mcp/, GET and POST, with the
    # client's credentials.
    local creds=(-H "Cookie: session=$COOKIE_VALUE" -H "Authorization: $AUTH_VALUE")
    fire mcp            "$ALLOWED" /mcp  "${creds[@]}"
    fire mcp-slash      "$ALLOWED" /mcp/ "${creds[@]}"
    fire mcp-post       "$ALLOWED" /mcp  "${creds[@]}" -X POST -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
    fire mcp-h11        "$ALLOWED" /mcp  "${creds[@]}" --http1.1
    # Case 3: every other path on the allowed name.
    fire path-api       "$ALLOWED" /api/x
    fire path-internal  "$ALLOWED" /internal/x
    fire path-perm      "$ALLOWED" "$PERMISSION_PATH?domain=$ALLOWED"
    fire path-root      "$ALLOWED" /
    fire path-public    "$ALLOWED" /mcp/public/x
    fire path-sub       "$ALLOWED" /mcp/x
    fire path-prefix    "$ALLOWED" /mcpx
    # Case 4: a refused name.
    fire denied         "$DENIED"  /mcp
    # Case 5: the server name is the allowed one, the Host is not.
    fire host-localhost "$ALLOWED" /mcp "${creds[@]}" -H "Host: localhost"
    fire host-local-h11 "$ALLOWED" /mcp "${creds[@]}" -H "Host: localhost" --http1.1
    fire host-ip        "$ALLOWED" /mcp "${creds[@]}" -H "Host: 127.0.0.1"
    fire host-main-dot  "$ALLOWED" /mcp "${creds[@]}" -H "Host: $MAIN_SITE."
    fire host-none-h10  "$ALLOWED" /mcp "${creds[@]}" -H "Host:" --http1.0

    # Let the last access-log lines land, then keep what the proxy printed.
    sleep 1
    docker logs "$NAME" > "$WORK/stdout.log" 2> "$WORK/stderr.log"
}

teardown_file() {
    if [ -n "${NAME:-}" ]; then
        docker rm -f "$NAME" "$NAME6" "${NAME6}-validate" > /dev/null 2>&1 || true
    fi
    [ -n "${WORK:-}" ] && rm -rf "$WORK"
    return 0
}

# Start a proxy on a config; publishes 443 on a free loopback port.
start_proxy() {   # $1 container name, $2 Caddyfile, $3... extra docker run arguments
    local name="$1" config="$2"
    shift 2
    docker run -d --name "$name" \
        -p 127.0.0.1::443 \
        --add-host api-blue:127.0.0.1 \
        "$@" \
        -v "$config:/etc/caddy/Caddyfile:ro" \
        -v "$WORK/extra:/opt/kagura-caddy-extra:ro" \
        "$IMAGE" > /dev/null
}

# Print the published port once Caddy says it serves the configuration. (A
# TLS probe cannot tell "not listening yet" from "listening, name refused":
# both end the handshake.)
wait_for_proxy() {   # $1 container name
    local port=""
    for _ in $(seq 1 40); do
        port="$(docker port "$1" 443/tcp 2> /dev/null | head -n 1)"
        port="${port##*:}"
        if [ -n "$port" ] && docker logs "$1" 2>&1 | grep -q 'serving initial configuration'; then
            break
        fi
        sleep 0.5
    done
    echo "$port"
}

# One request, recorded under $WORK/<id>.{status,exit,body,headers,err}.
fire_on() {   # $1 port, $2 case id, $3 TLS server name, $4 path, $5... curl args
    local port="$1" id="$2" name="$3" path="$4"
    shift 4
    local rc=0
    curl -sk --max-time 15 -A "live-case-$id" \
        --resolve "$name:$port:127.0.0.1" \
        -D "$WORK/$id.headers" -o "$WORK/$id.body" -w '%{http_code}' \
        "$@" "https://$name:$port$path" > "$WORK/$id.status" 2> "$WORK/$id.err" || rc=$?
    echo "$rc" > "$WORK/$id.exit"
}

fire() {   # $1 case id, $2 TLS server name, $3 path, $4... curl args
    fire_on "$PORT" "$@"
}

require_live() {
    [ -z "${LIVE_SKIP:-}" ] || skip "$LIVE_SKIP"
}

# Equality with a readable failure (bats prints only the failing line).
expect() {   # $1 actual, $2 wanted
    [ "$1" = "$2" ] || { echo "got:  $1"; echo "want: $2"; return 1; }
}

status_of() { cat "$WORK/$1.status"; }
exit_of()   { cat "$WORK/$1.exit"; }
body_of()   { cat "$WORK/$1.body" 2> /dev/null || true; }

# What the stand-in recorded about one case:
#   count | req_headers (sorted names) | resp:<Header>
standin_saw() {   # $1 case id, $2 what
    python3 - "$WORK/stdout.log" "live-case-$1" "$2" <<'PYEOF'
import json, sys

path, agent, what = sys.argv[1:4]
hits = []
for raw in open(path):
    try:
        d = json.loads(raw)
    except ValueError:
        continue
    if not isinstance(d, dict) or not str(d.get("logger", "")).startswith("http.log.access"):
        continue
    if d.get("request", {}).get("headers", {}).get("User-Agent") == [agent]:
        hits.append(d)
if what == "count":
    print(len(hits))
    sys.exit(0)
if len(hits) != 1:
    sys.exit(f"{agent}: {len(hits)} stand-in lines, want exactly 1")
d = hits[0]
if what == "req_headers":
    print(" ".join(sorted(d["request"]["headers"])))
elif what.startswith("resp:"):
    print(" ".join(d.get("resp_headers", {}).get(what[5:], [])))
else:
    sys.exit(f"unknown selector {what}")
PYEOF
}

# "<uri> <status>" of every permission request the stand-in answered.
permission_calls() {
    python3 - "$WORK/stdout.log" "$PERMISSION_PATH" <<'PYEOF'
import json, sys

path, prefix = sys.argv[1:3]
for raw in open(path):
    try:
        d = json.loads(raw)
    except ValueError:
        continue
    if not isinstance(d, dict) or not str(d.get("logger", "")).startswith("http.log.access"):
        continue
    uri = d.get("request", {}).get("uri", "")
    agent = d.get("request", {}).get("headers", {}).get("User-Agent", [""])[0]
    if uri.startswith(prefix) and not agent.startswith("live-case-"):
        print(uri, d.get("status"))
PYEOF
}

# ---------------------------------------------------------------------------
# The config under test is the real one
# ---------------------------------------------------------------------------

@test "live: the enabled render is deploy.sh's render of the operator's sed, minus the two scrub-suite lines" {
    require_live
    # The operator's sed removed exactly the marker lines' prefix.
    run bash -c 'diff "$1" "$2" | grep -c "^>"' _ "$TPL" "$WORK/enabled.tpl"
    expect "$output" "30"
    # 2 lines out (main site address, tls), 1 line in (the :9080 address).
    run bash -c 'diff "$1" "$2" | grep -c "^[<>]"' _ "$WORK/enabled.rendered" "$WORK/enabled.acme"
    expect "$output" "3"
    run bash -c 'diff "$1" "$2" | grep "^>"' _ "$WORK/enabled.rendered" "$WORK/enabled.acme"
    expect "$output" "> http://:9080 {"
}

@test "live: the request config differs from it only in the issuer (internal instead of acme)" {
    require_live
    run bash -c 'diff "$1" "$2" | grep "^[<>]"' _ "$WORK/enabled.acme" "$WORK/enabled.internal"
    expect "$output" "$(printf '< \t\tissuer acme {\n< \t\t\tdisable_http_challenge\n< \t\t}\n> \t\tissuer internal')"
    # The case-6 config (issuer as shipped) differs from the enabled render
    # only in the three global lines, left commented.
    run bash -c 'diff "$1" "$2" | grep -c "^[<>]"' _ "$WORK/enabled.acme" "$WORK/site-only.acme"
    expect "$output" "6"
    run bash -c 'diff "$1" "$2" | grep "^>" | grep -vc "^> #public-mcp-domains# "' _ "$WORK/enabled.acme" "$WORK/site-only.acme"
    expect "$output" "0"
}

# ---------------------------------------------------------------------------
# Case 1: the enabled render loads
# ---------------------------------------------------------------------------

@test "live 1: the enabled render (issuer acme) passes caddy validate in the image the stack runs" {
    require_live
    run docker run --rm --name "${NAME_PREFIX}validate-$SUFFIX" \
        -v "$WORK/enabled.acme:/etc/caddy/Caddyfile:ro" \
        -v "$WORK/extra:/opt/kagura-caddy-extra:ro" \
        "$IMAGE" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
    [ "$status" -eq 0 ] || { echo "$output"; return 1; }
    [[ "$output" == *"Valid configuration"* ]]
}

@test "live 1: the permission call targets the API upstream's permission path" {
    require_live
    run bash -c 'docker run --rm --name "$1" \
        -v "$2/enabled.acme:/etc/caddy/Caddyfile:ro" \
        -v "$2/extra:/opt/kagura-caddy-extra:ro" \
        "$3" caddy adapt --config /etc/caddy/Caddyfile --adapter caddyfile \
        > "$2/adapted.json" 2> "$2/adapt.err"' _ "${NAME_PREFIX}adapt-$SUFFIX" "$WORK" "$IMAGE"
    [ "$status" -eq 0 ] || { cat "$WORK/adapt.err"; return 1; }
    run python3 -c '
import json, sys
od = json.load(open(sys.argv[1]))["apps"]["tls"]["automation"]["on_demand"]
perm = od.get("permission") or {}
print(perm.get("module", "ask"), perm.get("endpoint") or od.get("ask"))
' "$WORK/adapted.json"
    [ "$status" -eq 0 ] || { echo "$output"; return 1; }
    expect "$output" "http http://api-blue:8080$PERMISSION_PATH"
}

# ---------------------------------------------------------------------------
# Case 2: an allowed name
# ---------------------------------------------------------------------------

@test "live 2: the proxy asks the permission path with ?domain=<name> itself" {
    require_live
    run permission_calls
    [ "$status" -eq 0 ] || { echo "$output"; return 1; }
    [[ "$output" == *"$PERMISSION_PATH?domain=$ALLOWED 200"* ]] || { echo "$output"; return 1; }
    [[ "$output" == *"$PERMISSION_PATH?domain=$DENIED 404"* ]] || { echo "$output"; return 1; }
}

@test "live 2: /mcp and /mcp/ on the allowed name reach the upstream with the client's Host" {
    require_live
    for c in mcp mcp-slash mcp-post mcp-h11; do
        expect "$(exit_of "$c")" "0"
        expect "$(status_of "$c")" "200"
        expect "$(body_of "$c")" "standin host=$ALLOWED cookie=[] authorization=[]"
        run standin_saw "$c" count
        expect "$output" "1"
    done
}

@test "live 2: the client's Cookie and Authorization do not arrive upstream" {
    require_live
    for c in mcp mcp-slash mcp-post mcp-h11; do
        run standin_saw "$c" req_headers
        [ "$status" -eq 0 ] || { echo "$output"; return 1; }
        [[ " $output " != *" Cookie "* ]] || { echo "$c: $output"; return 1; }
        [[ " $output " != *" Authorization "* ]] || { echo "$c: $output"; return 1; }
    done
    run grep -c -e "$COOKIE_VALUE" -e "kmc2809LiveAuthorizationValue" "$WORK/stdout.log"
    expect "$output" "0"
}

@test "live 2: the upstream's Set-Cookie does not come back to the client" {
    require_live
    for c in mcp mcp-slash mcp-post mcp-h11; do
        # The stand-in did send it on this very request (guard not vacuous;
        # Caddy's access log writes a cookie's value as REDACTED, so only
        # its presence is read)...
        run standin_saw "$c" resp:Set-Cookie
        [ "$status" -eq 0 ] && [ -n "$output" ] || { echo "$c: no Set-Cookie from the stand-in"; return 1; }
        # ...and the client did not receive it.
        run grep -ci '^set-cookie:' "$WORK/$c.headers"
        expect "$output" "0"
    done
}

# ---------------------------------------------------------------------------
# Case 3: any other path on the allowed name
# ---------------------------------------------------------------------------

@test "live 3: every other path on the allowed name is 404 and reaches no upstream" {
    require_live
    for c in path-api path-internal path-perm path-root path-public path-sub path-prefix; do
        expect "$(exit_of "$c")" "0"
        expect "$(status_of "$c")" "404"
        # Not the stand-in's 404 either.
        [[ "$(body_of "$c")" != *standin* ]] || { echo "$c: $(body_of "$c")"; return 1; }
        run standin_saw "$c" count
        expect "$output" "0"
    done
}

# ---------------------------------------------------------------------------
# Case 4: a refused name
# ---------------------------------------------------------------------------

@test "live 4: a name the permission path refuses fails the TLS handshake" {
    require_live
    [ "$(exit_of denied)" -ne 0 ] || { echo "status $(status_of denied)"; return 1; }
    expect "$(status_of denied)" "000"
    run standin_saw denied count
    expect "$output" "0"
}

# ---------------------------------------------------------------------------
# Case 5: Host differs from the TLS server name
# ---------------------------------------------------------------------------

@test "live 5: a Host other than the TLS server name is 404 (or 421) and reaches no upstream" {
    require_live
    [ -n "$MAIN_SITE" ]
    for c in host-localhost host-local-h11 host-ip host-main-dot host-none-h10; do
        expect "$(exit_of "$c")" "0"
        case "$(status_of "$c")" in
            404|421) ;;
            *) echo "$c: HTTP $(status_of "$c") $(body_of "$c")"; return 1 ;;
        esac
        [[ "$(body_of "$c")" != *standin* ]] || { echo "$c: $(body_of "$c")"; return 1; }
        run standin_saw "$c" count
        expect "$output" "0"
    done
}

# ---------------------------------------------------------------------------
# Case 6: the site without the permission lines
# ---------------------------------------------------------------------------

@test "live 6: the site without the global permission lines obtains no certificate" {
    require_live
    if [ "$(cat "$WORK/case6.validate.rc")" != "0" ]; then
        # The configuration does not load, and for this reason (Caddy's own
        # words), not for some unrelated error.
        grep -q 'without a permission module' "$WORK/case6.validate" || { cat "$WORK/case6.validate"; return 1; }
        echo "# case 6: the configuration does not load ($IMAGE, $IMAGE_VERSION): on-demand TLS without a permission module is refused" >&3
        return 0
    fi
    # It loads: then the handshake must fail and nothing reaches the stand-in.
    [ "$(exit_of case6)" -ne 0 ] || { echo "status $(status_of case6)"; return 1; }
    expect "$(status_of case6)" "000"
    echo "# case 6: the configuration loads and the handshake fails ($IMAGE, $IMAGE_VERSION)" >&3
}

@test "live: every container of this suite carries the run's name prefix" {
    require_live
    for n in "$NAME" "$NAME6"; do
        [[ "$n" == "$NAME_PREFIX"* ]] || { echo "$n"; return 1; }
    done
}
