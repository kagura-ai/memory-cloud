#!/usr/bin/env bats
# =============================================================================
# Optional Redis password for the single-server compose files (#1794).
#
# Redis starts with `--requirepass ${REDIS_PASSWORD:-}`: unset or empty is an
# empty requirepass — no auth, exactly as before — and a value makes Redis
# refuse unauthenticated commands. The API gets the password through
# REDIS_URL, which the env file may now set (it used to be a literal under
# `environment:`, which wins over `env_file:`).
#
# Two traps this suite pins:
#   - the command must stay exec-form with redis-server first: the image's
#     entrypoint drops root only for that shape, so a `sh -c` wrapper would
#     run Redis as root;
#   - `redis-cli ping` exits 0 on NOAUTH / WRONGPASS, so a healthcheck that
#     trusts the exit code stays "healthy" while every client is refused.
#
# The static tests render every documented composition. The live tests boot
# the redis service exactly as the render describes it (command, environment,
# healthcheck) in the image the compose files pin, and skip cleanly without
# docker or when the image cannot be pulled.
# =============================================================================

PROJECT_DIR="$BATS_TEST_DIRNAME/../.."

# Every class that breaks a naive URL (@ : / # %), a space and a double quote.
PASSWORD='k1794 p@ss:w/rd#%x"q'

setup_file() {
    export COMPOSE_MISSING=0 LIVE_SKIP=""
    if ! docker compose version > /dev/null 2>&1; then
        export COMPOSE_MISSING=1 LIVE_SKIP="docker compose not available"
        return 0
    fi

    WORK="$(mktemp -d)"
    export WORK
    mkdir -p "$WORK/single-server" "$WORK/backend" "$WORK/frontend"
    cp "$PROJECT_DIR"/docker-compose.prod.yml "$PROJECT_DIR"/docker-compose.data.yml \
       "$PROJECT_DIR"/docker-compose.app.yml "$PROJECT_DIR"/docker-compose.data-expose.yml \
       "$PROJECT_DIR"/docker-compose.ollama.yml "$WORK/single-server/"

    URL="redis://:$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$PASSWORD")@redis:6379"
    export URL
    cat > "$WORK/single-server/base.env" <<'ENVEOF'
DB_PASSWORD=redis-auth-test-pw
QDRANT_API_KEY=redis-auth-test-key
KAGURA_DOMAIN=memory.example.com
NEXT_PUBLIC_ENABLE_GRAPH_VIZ=
NEXT_PUBLIC_PLAN_DISPLAY_NAMES=
ENVEOF
    # Single quotes: the dotenv parser takes the value literally.
    { cat "$WORK/single-server/base.env"
      printf "REDIS_PASSWORD='%s'\nREDIS_URL='%s'\n" "$PASSWORD" "$URL"
    } > "$WORK/single-server/password.env"
    { cat "$WORK/single-server/base.env"; echo "REDIS_HOST=192.0.2.20"; } > "$WORK/single-server/host.env"

    # "<name>|<compose -f arguments>": the compositions README documents.
    export COMPOSITIONS="prod|-f docker-compose.prod.yml
data|-f docker-compose.data.yml
split|-f docker-compose.data.yml -f docker-compose.app.yml
ollama|-f docker-compose.prod.yml -f docker-compose.ollama.yml
expose|-f docker-compose.data.yml -f docker-compose.data-expose.yml"

    local env name files
    for env in base password host; do
        # As on the VM: the same file is the API's env_file and compose's
        # --env-file (deploy.sh always passes it, #643/#672).
        cp "$WORK/single-server/$env.env" "$WORK/single-server/.env.prod"
        while IFS='|' read -r name files; do
            # Scrubbed environment: an operator's exported secrets must neither
            # steer the render nor reach the test output. A failed render is
            # reported by the first test, not here.
            # shellcheck disable=SC2086  # $files is a deliberate word-split arg list
            (cd "$WORK/single-server" \
                && env -u QDRANT_API_KEY -u DB_PASSWORD -u KAGURA_DOMAIN \
                       -u POSTGRES_HOST -u QDRANT_HOST -u REDIS_HOST \
                       -u REDIS_PASSWORD -u REDIS_URL \
                       -u COMPOSE_PROJECT_NAME -u COMPOSE_FILE \
                       DATA_BIND_ADDR=192.0.2.10 \
                    docker compose -p single-server $files --env-file .env.prod \
                        config --format json) > "$WORK/$env-$name.json" 2> "$WORK/$env-$name.err" || true
        done <<< "$COMPOSITIONS"
    done

    # Live part: the image the compose files pin.
    IMAGE="$(awk '$1 == "image:" && $2 ~ /^redis:/ { print $2; exit }' "$PROJECT_DIR/docker-compose.data.yml")"
    export IMAGE
    if ! docker info > /dev/null 2>&1; then
        export LIVE_SKIP="docker daemon not reachable"
    elif [ -z "$IMAGE" ]; then
        export LIVE_SKIP="no redis image found in docker-compose.data.yml"
    elif ! docker image inspect "$IMAGE" > /dev/null 2>&1; then
        local pull=(docker pull -q "$IMAGE")
        if command -v timeout > /dev/null 2>&1; then pull=(timeout 120 "${pull[@]}"); fi
        "${pull[@]}" > /dev/null 2>&1 || export LIVE_SKIP="cannot pull $IMAGE"
    fi
    export TAG="k1794-$$-$RANDOM"
}

teardown_file() {
    if [ -n "${TAG:-}" ] && docker info > /dev/null 2>&1; then
        docker ps -aq --filter "name=^${TAG}-" | xargs -r docker rm -f > /dev/null 2>&1
        docker volume rm -f "${TAG}-data" > /dev/null 2>&1
    fi
    [ -n "${WORK:-}" ] && rm -rf "$WORK"
    return 0
}

require_compose() {
    [ "${COMPOSE_MISSING:-0}" = "0" ] || skip "docker compose not available"
}

require_live() {
    require_compose
    [ -z "${LIVE_SKIP:-}" ] || skip "$LIVE_SKIP"
}

# One field of a rendered service: svc_field <render> <service> <key.path>
# (dot-separated; integers index lists, -1 is the last item). A string prints
# raw, anything else as JSON.
svc_field() {
    python3 - "$WORK/$1.json" "$2" "$3" <<'PYEOF'
import json, sys
v = json.load(open(sys.argv[1]))["services"][sys.argv[2]]
for part in sys.argv[3].split("."):
    v = v[int(part)] if isinstance(v, list) else v[part]
print(v if isinstance(v, str) else json.dumps(v))
PYEOF
}

@test "every composition renders (guard not vacuous)" {
    require_compose
    local env name files
    for env in base password host; do
        while IFS='|' read -r name files; do
            [ -s "$WORK/$env-$name.json" ] || { cat "$WORK/$env-$name.err"; false; }
        done <<< "$COMPOSITIONS"
    done
}

@test "no REDIS_PASSWORD: Redis starts with an empty requirepass and no client auth" {
    require_compose
    local name
    for name in prod data split ollama expose; do
        run svc_field "base-$name" redis command
        [ "$output" = '["redis-server", "--appendonly", "yes", "--requirepass", ""]' ]
        run svc_field "base-$name" redis environment.REDISCLI_AUTH
        [ "$output" = "" ]
    done
}

@test "REDIS_PASSWORD reaches the server and the healthcheck unchanged" {
    require_compose
    local name
    for name in prod data split ollama expose; do
        run svc_field "password-$name" redis command.-1
        [ "$output" = "$PASSWORD" ]
        run svc_field "password-$name" redis environment.REDISCLI_AUTH
        [ "$output" = "$PASSWORD" ]
    done
}

@test "the command stays exec-form with redis-server first (the image drops root only then)" {
    require_compose
    local name
    for name in prod data split ollama expose; do
        run svc_field "password-$name" redis command.0
        [ "$output" = "redis-server" ]
    done
}

@test "the healthcheck matches the reply, not redis-cli's exit code" {
    require_compose
    local name
    for name in prod data; do
        run svc_field "password-$name" redis healthcheck.test.0
        [ "$output" = "CMD-SHELL" ]
        run svc_field "password-$name" redis healthcheck.test.1
        [[ "$output" == *"grep -q PONG"* ]]
    done
}

@test "REDIS_URL from the env file reaches both API colors in both layouts" {
    require_compose
    local name svc
    for name in prod split ollama; do
        for svc in api-blue api-green; do
            run svc_field "password-$name" "$svc" environment.REDIS_URL
            [ "$output" = "$URL" ]
        done
    done
}

@test "unset, REDIS_URL is still built from REDIS_HOST" {
    require_compose
    run svc_field "host-split" api-blue environment.REDIS_URL
    [ "$output" = "redis://192.0.2.20:6379" ]
    run svc_field "base-prod" api-blue environment.REDIS_URL
    [ "$output" = "redis://redis:6379" ]
}

@test "with a password set, the single-host and split renders still agree" {
    require_compose
    run python3 - "$WORK/password-prod.json" "$WORK/password-split.json" <<'PYEOF'
import json, sys
single, split = (json.load(open(p)) for p in sys.argv[1:3])
for doc in (single, split):
    doc.pop("x-api-common", None)
    for svc in ("api-blue", "api-green"):
        doc["services"][svc].pop("depends_on", None)
print("IDENTICAL" if single == split else "DIFFERENT")
PYEOF
    [ "$output" = "IDENTICAL" ]
}

# --- live --------------------------------------------------------------------

# Start the redis service as the given render describes it. Prints the name.
# start_redis <render> <name suffix> [extra docker run args...]
start_redis() {
    local render="$1" name="${TAG}-$2"
    shift 2
    local -a cmd
    mapfile -d '' -t cmd < <(python3 - "$WORK/$render.json" <<'PYEOF'
import json, sys
s = json.load(open(sys.argv[1]))["services"]["redis"]
sys.stdout.write("\0".join(s["command"]) + "\0")
PYEOF
    )
    local health auth
    # Compose writes a literal $ as $$ in the render and unescapes it when it
    # creates the container; docker run takes the unescaped form.
    health="$(svc_field "$render" redis healthcheck.test.1)"
    health="${health//\$\$/\$}"
    auth="$(svc_field "$render" redis environment.REDISCLI_AUTH)"
    docker run -d --name "$name" -p 127.0.0.1::6379 -e "REDISCLI_AUTH=$auth" \
        --health-cmd "$health" --health-interval 1s --health-timeout 3s --health-retries 30 \
        "$@" "$IMAGE" "${cmd[@]}" > /dev/null
    echo "$name"
}

wait_healthy() {
    local status=""
    for _ in $(seq 1 60); do
        status="$(docker inspect -f '{{.State.Health.Status}}' "$1" 2> /dev/null)"
        [ "$status" = "healthy" ] && return 0
        sleep 0.5
    done
    echo "health: ${status:-none}"; docker logs "$1" 2>&1 | tail -5
    return 1
}

# RESP over a socket with the password the API would take from REDIS_URL
# (redis-py percent-decodes it the same way). Prints each reply line.
# resp_via_url <container> <url> <command words...>
resp_via_url() {
    local port
    port="$(docker port "$1" 6379/tcp | head -n 1)"
    port="${port##*:}"
    python3 - "$port" "$2" "${@:3}" <<'PYEOF'
import socket, sys, urllib.parse
port, url, words = int(sys.argv[1]), sys.argv[2], sys.argv[3:]
password = urllib.parse.unquote(urllib.parse.urlsplit(url).password or "")

def cmd(*args):
    out = f"*{len(args)}\r\n".encode()
    for a in args:
        b = a.encode()
        out += b"$%d\r\n%s\r\n" % (len(b), b)
    return out

with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
    if password:
        s.sendall(cmd("AUTH", password))
        print(s.recv(4096).decode().strip())
    s.sendall(cmd(*words))
    print(s.recv(4096).decode().strip())
PYEOF
}

@test "live: with a password, unauthenticated commands are refused and the healthcheck is healthy" {
    require_live
    local c
    c="$(start_redis password-data auth)"
    wait_healthy "$c"
    run docker exec "$c" sh -c 'unset REDISCLI_AUTH; redis-cli ping'
    [[ "$output" == *NOAUTH* ]]
    run docker exec -e REDISCLI_AUTH=wrong "$c" redis-cli ping
    [[ "$output" != *PONG* ]]
}

@test "live: the URL-encoded password in REDIS_URL authenticates" {
    require_live
    local c
    c="${TAG}-auth"
    docker inspect "$c" > /dev/null 2>&1 || { c="$(start_redis password-data auth)"; wait_healthy "$c"; }
    run resp_via_url "$c" "$URL" PING
    [ "${lines[0]}" = "+OK" ]
    [ "${lines[1]}" = "+PONG" ]
}

@test "live: without a password the healthcheck is healthy and sends no failed AUTH" {
    require_live
    local c
    c="$(start_redis base-data open)"
    wait_healthy "$c"
    run docker exec "$c" sh -c 'unset REDISCLI_AUTH; redis-cli ping'
    [ "$output" = "PONG" ]
    run docker inspect -f '{{range .State.Health.Log}}{{.Output}}{{end}}' "$c"
    [[ "$output" != *"AUTH failed"* ]]
}

@test "live: turning auth on keeps sessions and spend counters (AOF survives the restart)" {
    require_live
    local vol="${TAG}-data" c
    docker volume create "$vol" > /dev/null
    c="$(start_redis base-data before -v "$vol:/data")"
    wait_healthy "$c"
    docker exec "$c" redis-cli set session:k1794 alive EX 3600 > /dev/null
    docker exec "$c" redis-cli incrby embed_spend:k1794 7 > /dev/null
    docker stop -t 10 "$c" > /dev/null    # SIGTERM: what compose sends on recreate
    docker rm "$c" > /dev/null

    c="$(start_redis password-data after -v "$vol:/data")"
    wait_healthy "$c"
    run docker exec "$c" redis-cli get session:k1794
    [ "$output" = "alive" ]
    run docker exec "$c" redis-cli ttl session:k1794
    [ "$output" -gt 0 ]
    run docker exec "$c" redis-cli get embed_spend:k1794
    [ "$output" = "7" ]
}
