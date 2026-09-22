#!/usr/bin/env bats
# =============================================================================
# Port-publishing guard (#1626): no compose file in the repo may publish a
# data-store port — PostgreSQL 5432, Qdrant 6333/6334, Redis 6379, MinIO
# 9000/9001 — on every interface.
#
# A two-part "HOST:CONTAINER" entry makes Docker bind 0.0.0.0 and [::], and on
# Linux the nat rules Docker inserts run before the chains ufw filters, so a
# two-part entry on a laptop on a shared network hands the dev databases (no
# Redis/Qdrant auth, the .env.example postgres password) to the whole LAN. The
# dev compose therefore publishes them as "${COMPOSE_BIND_HOST:-127.0.0.1}:P:P";
# the split-host overlay as "${DATA_BIND_ADDR:?...}:P:P" (#1513); the prod
# files publish nothing but Caddy 80/443.
#
# Static part: every tracked *compose*.yml, bash + awk only — runs wherever the
# CI shell job runs. Render part: `docker compose config` on the dev file,
# skipped when compose is missing (same convention as compose_tier_split_parity).
#
# Only the short "HOST:PUBLISHED:TARGET" form is readable by the static scan;
# a long-syntax `target:` on a data-store port fails the guard deliberately.
# =============================================================================

REPO_ROOT="$BATS_TEST_DIRNAME/../../../.."
DATA_PORTS='5432|6333|6334|6379|9000|9001'

# Tracked compose files, relative to the repo root (git pathspec `*` crosses
# directory separators, so this finds terraform/single-server/*.yml too).
compose_files() {
    git -C "$REPO_ROOT" ls-files -- '*compose*.yml'
}

# Print "file<TAB>line<TAB>entry" for every item of every `ports:` block.
# An item may sit deeper than the key or, as YAML allows, at the same indent.
ports_entries() {
    awk '
        function indent(s) { match(s, /^[ ]*/); return RLENGTH }
        FNR == 1 { inports = 0 }
        /^[ ]*#/ || /^[ ]*$/ { next }
        inports && (indent($0) < pindent || (indent($0) == pindent && $0 !~ /^[ ]*-/)) { inports = 0 }
        inports {
            line = $0
            sub(/^[ ]*-[ ]*/, "", line); sub(/[ ]*$/, "", line); gsub(/"/, "", line)
            printf "%s\t%d\t%s\n", FILENAME, FNR, line
            next
        }
        /^[ ]*ports:[ ]*$/ { inports = 1; pindent = indent($0) }
    ' "$@"
}

# Verdict for one entry: OK, SKIP (not a data-store port), or "BAD <reason>".
classify() {
    local e="$1"
    if [[ "$e" =~ ^target:[[:space:]]*(${DATA_PORTS})$ ]]; then
        echo "BAD long syntax is not checked here; use the HOST:PORT:PORT form"; return
    fi
    [[ "$e" == *": "* ]] && { echo SKIP; return; }      # other long-syntax keys
    local cport="${e##*:}"
    [[ "$cport" =~ ^(${DATA_PORTS})(/(tcp|udp))?$ ]] || { echo SKIP; return; }
    local rest="${e%:*}"                                 # HOST:PUBLISHED or PUBLISHED
    if [[ "$rest" =~ ^[0-9]+(-[0-9]+)?$ ]]; then
        echo "BAD no host address: Docker binds 0.0.0.0 and [::]"; return
    fi
    local pub="${rest##*:}" host="${rest%:*}"
    [[ "$pub" =~ ^[0-9]+(-[0-9]+)?$ ]] || { echo "BAD unrecognised entry shape"; return; }
    case "$host" in
        0.0.0.0|'[::]'|::) echo "BAD host address $host is every interface"; return ;;
    esac
    local var_re='^\$\{([A-Za-z_][A-Za-z0-9_]*)(:?[-?+])?(.*)\}$'
    if [[ "$host" =~ $var_re ]]; then
        local name="${BASH_REMATCH[1]}" op="${BASH_REMATCH[2]}" dflt="${BASH_REMATCH[3]}"
        case "$op" in
            :\?|\?) echo OK ;;                            # required: compose refuses to render without it
            :-|-)
                case "$dflt" in
                    ''|0.0.0.0|'[::]'|::) echo "BAD default '$dflt' is every interface" ;;
                    *) echo OK ;;
                esac ;;
            *) echo "BAD \${$name} has no default: unset binds every interface" ;;
        esac
        return
    fi
    echo OK
}

# Scan files; print "OK|BAD<TAB>file:line<TAB>entry<TAB>reason" per data-store
# entry and exit 1 when any is BAD.
scan() {
    local bad=0 f n e verdict
    while IFS=$'\t' read -r f n e; do
        verdict="$(classify "$e")"
        [ "$verdict" = SKIP ] && continue
        printf '%s\t%s:%s\t%s\t%s\n' "${verdict%% *}" "$f" "$n" "$e" "${verdict#* }"
        [ "${verdict%% *}" = BAD ] && bad=$((bad + 1))
    done < <(ports_entries "$@")
    [ "$bad" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Static guard
# ---------------------------------------------------------------------------

@test "no tracked compose file publishes a data-store port on every interface" {
    cd "$REPO_ROOT"
    mapfile -t files < <(compose_files)
    [ "${#files[@]}" -ge 1 ]
    run scan "${files[@]}"
    [ "$status" -eq 0 ]
}

@test "the data-store entries are present (guard not vacuous)" {
    # 6 dev entries (5432, 6333, 6334, 6379, 9000, 9001) + 3 in the split-host
    # data-expose overlay. If an entry is added or dropped, move this floor
    # deliberately.
    cd "$REPO_ROOT"
    mapfile -t files < <(compose_files)
    run scan "${files[@]}"
    [ "$status" -eq 0 ]
    [ "$(printf '%s\n' "$output" | grep -c '^OK')" -ge 9 ]
}

@test "the scanner rejects every all-interfaces shape (self-check)" {
    # The mutation check the guard promises: each of these is what an edit
    # back to the old form would look like. Fixture only — the repo is untouched.
    local fixture="$FIXTURES/bad-compose.yml"
    cat > "$fixture" <<'YAML'
services:
  postgres:
    ports:
      - "5432:5432"
      - "0.0.0.0:5432:5432"
      - "[::]:5432:5432"
      - "${X:-0.0.0.0}:5432:5432"
      - "${X:-}:5432:5432"
      - "${X}:5432:5432"
      - target: 5432
        published: 5432
  api:
    ports:
      - "8080:8080"
YAML
    run scan "$fixture"
    [ "$status" -ne 0 ]
    [ "$(printf '%s\n' "$output" | grep -c '^BAD')" -eq 7 ]
    [ "$(printf '%s\n' "$output" | grep -c '^OK')" -eq 0 ]
    [[ "$output" != *"8080"* ]]                           # non-data ports are not the guard's business
}

@test "the scanner accepts the loopback and required-variable shapes (self-check)" {
    local fixture="$FIXTURES/ok-compose.yml"
    cat > "$fixture" <<'YAML'
services:
  postgres:
    ports:
    - "127.0.0.1:5432:5432"
    - "[::1]:5432:5432"
    - "${COMPOSE_BIND_HOST:-127.0.0.1}:6379:6379"
    - "${DATA_BIND_ADDR:?DATA_BIND_ADDR must be set to the data VM private IP}:6333:6333"
    - "192.168.10.20:9000:9000/tcp"
YAML
    run scan "$fixture"
    [ "$status" -eq 0 ]
    [ "$(printf '%s\n' "$output" | grep -c '^OK')" -eq 5 ]
}

# ---------------------------------------------------------------------------
# Render check — what compose actually resolves the dev file to
# ---------------------------------------------------------------------------

setup_file() {
    FIXTURES="$(mktemp -d)"               # self-check fixtures (static part)
    export FIXTURES

    if ! docker compose version > /dev/null 2>&1; then
        export COMPOSE_MISSING=1
        return 0
    fi
    export COMPOSE_MISSING=0

    WORK="$(mktemp -d)"
    export WORK
    # Only the compose file is copied: a developer's project .env must not leak
    # into the render. The api service's env_file has to exist; the build
    # context and bind-mount sources only have to be paths.
    mkdir -p "$WORK/backend" "$WORK/frontend"
    cp "$REPO_ROOT/docker-compose.yml" "$WORK/"
    : > "$WORK/.env.local"

    # --profile minio so the opt-in 9000/9001 entries are rendered too. The
    # first argument list is extra `VAR=value` pairs for the escape-hatch check.
    render() {
        (cd "$WORK" \
            && env -u COMPOSE_BIND_HOST -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME "$@" \
                docker compose -p portbind --profile minio config --format json)
    }
    render                          > "$WORK/default.json"  2>/dev/null
    render COMPOSE_BIND_HOST=0.0.0.0 > "$WORK/override.json" 2>/dev/null
}

teardown_file() {
    [ -n "${FIXTURES:-}" ] && rm -rf "$FIXTURES"
    [ -n "${WORK:-}" ] && rm -rf "$WORK"
    return 0
}

require_compose() {
    [ "${COMPOSE_MISSING:-0}" = "0" ] || skip "docker compose not available"
}

# "service target host_ip" per published data-store port, then per api/web port.
# host_ip is absent from the render when an entry has no host address.
host_ips() {
    python3 - "$1" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1]))
for svc in ("postgres", "qdrant", "redis", "minio", "api", "web"):
    for p in doc["services"][svc]["ports"]:
        print(svc, p["target"], p.get("host_ip", "-"))
PYEOF
}

@test "the dev compose renders every data-store port on 127.0.0.1 and leaves api/web alone" {
    require_compose
    run host_ips "$WORK/default.json"
    [ "$status" -eq 0 ]
    [ "$output" = "$(cat <<'EOF'
postgres 5432 127.0.0.1
qdrant 6333 127.0.0.1
qdrant 6334 127.0.0.1
redis 6379 127.0.0.1
minio 9000 127.0.0.1
minio 9001 127.0.0.1
api 8080 -
web 3000 -
EOF
)" ]
}

@test "COMPOSE_BIND_HOST overrides the bind address (escape hatch)" {
    require_compose
    run host_ips "$WORK/override.json"
    [ "$status" -eq 0 ]
    [ "$(printf '%s\n' "$output" | grep -c ' 0.0.0.0$')" -eq 6 ]
    [[ "$output" == *"api 8080 -"* ]]
}
