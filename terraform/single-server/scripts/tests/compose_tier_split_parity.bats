#!/usr/bin/env bats
# =============================================================================
# Drift guard for the app/data tier split (#1513).
#
# docker-compose.prod.yml (single host, the GCE layout) and the pair
# docker-compose.data.yml + docker-compose.app.yml (split host, the Sakura
# layout) describe the same stack twice. This suite pins that the two
# compositions render to the SAME topology, so a change made to one and
# forgotten in the other fails CI instead of surfacing during a migration.
#
# The only tolerated difference is the `depends_on` edges from the API services
# to the databases: they are impossible to express across two hosts, and on the
# split layout ordering is covered by `restart: always` plus deploy.sh's
# /readiness gate (which checks Postgres, Qdrant and Redis before any switch).
#
# Renders happen in a temp dir with a synthetic .env.prod and a scrubbed
# environment — a real QDRANT_API_KEY exported in the operator's shell would
# otherwise override the env file and leak into the diff output.
# =============================================================================

PROJECT_DIR="$BATS_TEST_DIRNAME/../.."

setup_file() {
    if ! docker compose version > /dev/null 2>&1; then
        export COMPOSE_MISSING=1
        return 0
    fi
    export COMPOSE_MISSING=0

    WORK="$(mktemp -d)"
    export WORK
    mkdir -p "$WORK/single-server" "$WORK/backend" "$WORK/frontend"
    cp "$PROJECT_DIR"/docker-compose.prod.yml \
       "$PROJECT_DIR"/docker-compose.data.yml \
       "$PROJECT_DIR"/docker-compose.app.yml "$WORK/single-server/"

    cat > "$WORK/single-server/.env.prod" <<'ENVEOF'
DB_PASSWORD=parity-test-pw
QDRANT_API_KEY=parity-test-key
KAGURA_DOMAIN=memory.example.com
NEXT_PUBLIC_ENABLE_GRAPH_VIZ=
NEXT_PUBLIC_PLAN_DISPLAY_NAMES=
ENVEOF

    # Scrub anything from the ambient shell that compose would prefer over the
    # env file, so the two renders are comparable (and no real key is printed).
    render() {
        (cd "$WORK/single-server" && env -u QDRANT_API_KEY -u DB_PASSWORD -u KAGURA_DOMAIN \
            docker compose -p single-server "$@" --env-file .env.prod config)
    }
    render -f docker-compose.prod.yml                              > "$WORK/single.yml" 2>/dev/null
    render -f docker-compose.data.yml -f docker-compose.app.yml    > "$WORK/split.yml"  2>/dev/null
}

teardown_file() {
    [ -n "${WORK:-}" ] && rm -rf "$WORK"
    return 0
}

require_compose() {
    [ "${COMPOSE_MISSING:-0}" = "0" ] || skip "docker compose not available"
}

# Compare the two renders, dropping the documented exception.
diff_topology() {
    python3 - "$WORK/single.yml" "$WORK/split.yml" <<'PYEOF'
import sys, yaml, json

single, split = (yaml.safe_load(open(p)) for p in sys.argv[1:3])
for doc in (single, split):
    doc.pop("x-api-common", None)            # anchor block, not a service
    for svc in ("api-blue", "api-green"):    # cross-host depends_on: documented exception
        doc["services"][svc].pop("depends_on", None)

if single == split:
    print("IDENTICAL")
else:
    def walk(a, b, path=""):
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a) | set(b)):
                if k not in a:
                    print(f"{path}.{k}: only in split")
                elif k not in b:
                    print(f"{path}.{k}: only in single-host")
                else:
                    walk(a[k], b[k], f"{path}.{k}")
        elif a != b:
            print(f"{path}: {json.dumps(a)[:60]} != {json.dumps(b)[:60]}")
    walk(single, split)
PYEOF
}

@test "split and single-host renders describe the same topology" {
    require_compose
    run diff_topology
    [ "$status" -eq 0 ]
    [ "$output" = "IDENTICAL" ]
}

@test "the shared docker network name is single-server_default in both" {
    require_compose
    for f in "$WORK/single.yml" "$WORK/split.yml"; do
        run python3 -c "import yaml,sys; print(yaml.safe_load(open(sys.argv[1]))['networks']['default']['name'])" "$f"
        [ "$status" -eq 0 ]
        [ "$output" = "single-server_default" ]
    done
}

@test "data-tier hostnames default to the compose service names" {
    require_compose
    run python3 -c "
import yaml,sys
e=yaml.safe_load(open(sys.argv[1]))['services']['api-blue']['environment']
print(e['DATABASE_URL'].split('@')[1], e['QDRANT_URL'], e['REDIS_URL'])
" "$WORK/split.yml"
    [ "$status" -eq 0 ]
    [ "$output" = "postgres:5432/kagura http://qdrant:6333 redis://redis:6379" ]
}

@test "the data VM overlay refuses to publish without an explicit bind address" {
    require_compose
    # DATA_BIND_ADDR is unset here: compose must fail rather than bind 0.0.0.0.
    run bash -c "cd '$WORK/single-server' && cp '$PROJECT_DIR/docker-compose.data-expose.yml' . && \
        env -u DATA_BIND_ADDR docker compose -p single-server \
        -f docker-compose.data.yml -f docker-compose.data-expose.yml --env-file .env.prod config"
    [ "$status" -ne 0 ]
    [[ "$output" == *"DATA_BIND_ADDR"* ]]
}
