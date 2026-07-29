#!/usr/bin/env bash
# =============================================================================
# Kagura Memory Cloud — Blue-Green Deploy Script (Issue #239)
# =============================================================================
# Zero-downtime deployment for the API container. Switches between api-blue
# and api-green while keeping Caddy routing live.
#
# The frontend (kagura-web) is rebuilt in place via --web; its
# NEXT_PUBLIC_* build args are baked into the bundle at build time, so
# blue-green doesn't apply. Funneling the rebuild through dc() guarantees
# --env-file .env.prod is always present (root cause of #643/#672).
#
# Usage:
#   ./scripts/deploy.sh              # normal API blue-green deploy
#   ./scripts/deploy.sh --rollback   # switch back to previous API color
#   ./scripts/deploy.sh --status     # show current active API color
#   ./scripts/deploy.sh --web        # rebuild + restart kagura-web in place
#
# Requires (on host): docker compose, envsubst (gettext-base)
#   Inside images: curl (api container), node (kagura-web container)
#   Additionally for --web (host): timeout (coreutils) — checked at invocation, not at startup
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$PROJECT_DIR/docker-compose.prod.yml"
CADDYFILE_TPL="${CADDYFILE_TPL:-$PROJECT_DIR/Caddyfile.tpl}"
CADDYFILE="$PROJECT_DIR/Caddyfile"
MARKER_FILE="/opt/kagura-memory/active-color"
ENV_FILE="$PROJECT_DIR/.env.prod"

READINESS_TIMEOUT="${READINESS_TIMEOUT:-60}"   # seconds to wait for /readiness
READINESS_INTERVAL=2                            # seconds between checks
DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-30}"            # seconds to drain old container
WEB_READINESS_TIMEOUT="${WEB_READINESS_TIMEOUT:-30}"  # seconds to wait for kagura-web /api/health
WEB_READINESS_INTERVAL="${WEB_READINESS_INTERVAL:-2}" # seconds between web checks
WORKERS_GATE_TIMEOUT="${WORKERS_GATE_TIMEOUT:-30}"    # seconds to wait for an edge-blocked path (/api/v1/workers/*, /internal/*) to 404 at Caddy
WORKERS_GATE_INTERVAL="${WORKERS_GATE_INTERVAL:-2}"   # seconds between security-gate checks (shared by the workers + internal gates)

# curl indirection: the security-gate probe runs through "$CURL" so the bats
# suite can inject a stub (tests/deploy_verify_workers_blocked.bats). Production
# leaves it unset and resolves to the real curl — behaviour is unchanged.
CURL="${CURL:-curl}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()   { echo "[deploy] $(date -u +%H:%M:%S) $*"; }
error() { echo "[deploy] ERROR: $*" >&2; exit 1; }

dc() { docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }

# Tracks the current step so the EXIT trap can report *where* a run aborted.
# Updated at the head of each step; read only by on_exit().
DEPLOY_STAGE="startup"

# Single final-status line on EVERY exit path (success or abort), so a run can
# never "exit 0-ish" silently again (#986). error() and any set -e death both
# route through here because the trap fires on EXIT regardless of cause.
on_exit() {
    local code=$1
    if [ "$code" -ne 0 ]; then
        echo "[deploy] ERROR: === FINAL STATUS: ABORTED at '${DEPLOY_STAGE}' (exit ${code}) ===" >&2
    elif [ "$DEPLOY_STAGE" != "startup" ] && [ "$DEPLOY_STAGE" != "readonly" ]; then
        # Read-only commands (--status/--help) set DEPLOY_STAGE=readonly and stay
        # quiet on success; only the mutating flows announce a clean finish.
        log "=== FINAL STATUS: SUCCESS (${DEPLOY_STAGE}) ==="
    fi
}

# The marker names the color that is LIVE right now — never the one a switch is
# heading toward. Every writer updates it only AFTER the new color is up and has
# passed its readiness check (deploy Step 5; rollback after wait_for_readiness),
# and the old color is drained only after Caddy has been pointed away. Keep that
# order: writing the marker first would publish a color that cannot serve, and
# an interrupted run would leave it that way (#1448).
get_active_color() {
    # A missing marker used to default to "blue" silently. That is the one
    # failure mode this file cannot absorb: with no marker, every reader
    # *agrees* on a color nobody selected, so a host whose marker was lost
    # (fresh volume, interrupted bootstrap, unclean shutdown truncating the
    # file) looks healthy while Caddy and the deploy script disagree with
    # reality. The bootstrap in README.md writes it explicitly; if it is gone,
    # say so instead of guessing (#1448).
    if [ ! -s "$MARKER_FILE" ]; then
        error "Marker file $MARKER_FILE is missing or empty. It must name the
       color serving traffic right now. Recover with:
           docker ps --format '{{.Names}}' | grep kagura-api-
           echo <blue|green> > $MARKER_FILE"
    fi
    local color
    color="$(cat "$MARKER_FILE")"
    # Validate — must be exactly "blue" or "green"
    if [ "$color" != "blue" ] && [ "$color" != "green" ]; then
        error "Marker file $MARKER_FILE contains invalid value: '$color'. Expected 'blue' or 'green'."
    fi
    echo "$color"
}

# Report a marker that names a color with no running container (#1448).
#
# That state means a switch was interrupted after the marker moved but before
# the color it names came up — or that the color died afterwards. It is not
# cosmetic: `get_inactive_color` is derived from the marker, so the next deploy
# would build the color that IS live and drain the one that is not. Downstream,
# kagura-bridge's connect-level cross-color fallback (kagura-ai/kagura-bridge#211)
# masks the mismatch instead of surfacing it — it absorbed 4162 calls over 65
# hours in the 2026-07-21..24 incident, turning a safety net into the normal
# path and burying every other warning in its noise.
#
# Reports rather than exits: this runs on the read-only status path, where the
# operator needs the diagnosis, not a dead command.
check_marker_matches_live() {
    local color="$1"
    if is_container_running "$color"; then
        return 0
    fi
    log "WARNING: marker says '$color' but kagura-api-${color} is NOT running."
    log "         A switch was likely interrupted, or the live color died."
    log "         Traffic is only being served via cross-color fallback, if at all."
    local other
    if [ "$color" = "blue" ]; then other="green"; else other="blue"; fi
    if is_container_running "$other"; then
        log "         kagura-api-${other} IS running — it is probably the real live color."
    fi
    return 1
}

get_inactive_color() {
    local active
    active="$(get_active_color)"
    if [ "$active" = "blue" ]; then echo "green"; else echo "blue"; fi
}

is_container_running() {
    local container="kagura-api-$1"
    docker inspect --format '{{.State.Running}}' "$container" 2>/dev/null | grep -q "true"
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
cmd_status() {
    local active
    active="$(get_active_color)"
    log "Active color: $active"
    log "Marker file:  $MARKER_FILE"

    # #1448: the whole point of a status command is to catch this before the
    # next deploy computes its target color from a marker that is lying.
    check_marker_matches_live "$active" || true

    # Show container states
    dc ps --format "table {{.Name}}\t{{.Status}}" 2>/dev/null \
        | grep -E "kagura-api|NAME" || true
}

cmd_rollback() {
    DEPLOY_STAGE="rollback: start"
    log "=== ROLLBACK ==="
    local active inactive
    active="$(get_active_color)"
    inactive="$(get_inactive_color)"

    # Ensure the previous color's container is running
    if ! is_container_running "$inactive"; then
        log "api-${inactive} is not running — starting it for rollback..."
        docker update --restart=always "kagura-api-${inactive}" 2>/dev/null || true
        # --no-deps: never recreate shared services (postgres/redis/qdrant)
        # whose compose config may have drifted from the running containers —
        # same guard as cmd_deploy_web (see the #1302 postgres footgun).
        dc up -d --no-deps "api-${inactive}"
        log "Waiting for api-${inactive} readiness..."
        wait_for_readiness "$inactive"
    fi

    # Write marker BEFORE switching Caddy (atomic: same filesystem mv)
    log "Switching Caddy: api-${active} -> api-${inactive}"
    echo "$inactive" > "${MARKER_FILE}.tmp"
    mv "${MARKER_FILE}.tmp" "$MARKER_FILE"
    DEPLOY_STAGE="rollback: switch Caddy -> api-${inactive} (incl. security gate)"
    generate_caddyfile "api-${inactive}"
    reload_caddy
    verify_workers_blocked
    verify_internal_blocked

    DEPLOY_STAGE="rollback complete (active: ${inactive})"
    log "Rollback complete. Active: $inactive"
}

cmd_deploy() {
    log "=== BLUE-GREEN DEPLOY ==="
    local active inactive
    active="$(get_active_color)"
    inactive="$(get_inactive_color)"
    log "Current active: $active — deploying to: $inactive"

    # #1448: both colors below are derived from the marker. If it names a color
    # that is not running, this deploy would build the color that IS serving and
    # drain the one that is not — the opposite of what was asked. Surface it and
    # stop; recovering is a one-line marker write, guessing is not recoverable.
    if ! check_marker_matches_live "$active"; then
        error "Refusing to deploy from a marker that does not match reality.
       Point $MARKER_FILE at the color actually serving traffic, then re-run."
    fi

    # Step 1: Build new image
    DEPLOY_STAGE="Step 1/7: build api-${inactive} image"
    log "Step 1/7: Building api-${inactive} image..."
    dc build "api-${inactive}"

    # Step 2: Start the inactive color (uses new image)
    # Re-enable restart policy in case it was disabled by a previous deploy.
    DEPLOY_STAGE="Step 2/7: start api-${inactive}"
    log "Step 2/7: Starting api-${inactive}..."
    # --no-deps: a routine deploy must never recreate postgres/redis/qdrant.
    # After #1302 the postgres service definition (image + volume) changed;
    # without this flag a plain deploy would recreate postgres onto the empty
    # PG18 volume and the deploy would still report green. Database cutover is
    # exclusively the runbook's job: docs/ops/postgres-18-migration-runbook.md
    dc up -d --no-deps "api-${inactive}"
    docker update --restart=always "kagura-api-${inactive}" 2>/dev/null || true

    # Step 3: Wait for readiness (DB + Qdrant + Redis all reachable)
    DEPLOY_STAGE="Step 3/7: wait for api-${inactive} readiness"
    log "Step 3/7: Waiting for api-${inactive} readiness (timeout: ${READINESS_TIMEOUT}s)..."
    wait_for_readiness "$inactive"

    # Step 4: Run Alembic migrations on the NEW container
    # Forward-compatible by convention: both old and new code work with new schema.
    DEPLOY_STAGE="Step 4/7: alembic upgrade on api-${inactive}"
    log "Step 4/7: Running database migrations on api-${inactive}..."
    dc exec -T "api-${inactive}" alembic upgrade head

    # Step 5: Write marker BEFORE switching Caddy (crash-safe ordering)
    DEPLOY_STAGE="Step 5/7: update marker -> ${inactive}"
    log "Step 5/7: Updating marker -> ${inactive}"
    echo "$inactive" > "${MARKER_FILE}.tmp"
    mv "${MARKER_FILE}.tmp" "$MARKER_FILE"

    # Step 6: Switch Caddy upstream
    DEPLOY_STAGE="Step 6/7: switch Caddy upstream -> api-${inactive} (incl. security gate)"
    log "Step 6/7: Switching Caddy upstream -> api-${inactive}..."
    generate_caddyfile "api-${inactive}"
    reload_caddy
    verify_workers_blocked
    verify_internal_blocked

    # Step 7: Drain and stop old container
    # Disable restart policy first — otherwise `restart: always` revives
    # the container immediately after stop.
    DEPLOY_STAGE="Step 7/7: drain + stop api-${active}"
    log "Step 7/7: Draining api-${active} for ${DRAIN_TIMEOUT}s..."
    sleep "$DRAIN_TIMEOUT"
    docker update --restart=no "kagura-api-${active}" 2>/dev/null || true
    dc stop "api-${active}" || true
    log "api-${active} stopped (restart policy disabled)."

    DEPLOY_STAGE="deploy complete (active: ${inactive})"
    log "=== DEPLOY COMPLETE ==="
    log "Active: $inactive"
    log "To rollback: $0 --rollback"
}

cmd_deploy_web() {
    # `timeout` (coreutils) is only used by this command path. Gate the
    # check here so default (API blue-green), --rollback, and --status
    # remain usable on environments without `timeout` installed.
    command -v timeout > /dev/null 2>&1 \
        || error "timeout not found (required by --web). Install GNU coreutils: apt-get install coreutils"

    DEPLOY_STAGE="web: rebuild + restart kagura-web"
    log "=== FRONTEND REBUILD (in-place) ==="
    log "Note: brief downtime expected during container restart (build is non-blocking; the running container keeps serving until --force-recreate)."

    # Step 1: --no-cache — NEXT_PUBLIC_* build args are baked into the layer;
    # a cached layer would silently carry stale values from a prior build.
    log "Step 1/3: Building kagura-web image (--no-cache, typically 3-5 min)..."
    dc build --no-cache web

    # Step 2: In-place restart. --no-deps prevents compose from recreating
    # shared services (postgres/redis/qdrant/caddy/api-*) — see memory
    # savepoint 9389da56 for the footgun this guards against.
    log "Step 2/3: Restarting kagura-web (--no-deps --force-recreate)..."
    dc up -d --no-deps --force-recreate web

    # Step 3: Smoke check from inside the container — the Dockerfile
    # HEALTHCHECK uses this same endpoint, so we ride that contract.
    log "Step 3/3: Waiting for kagura-web readiness (timeout: ${WEB_READINESS_TIMEOUT}s)..."
    wait_for_web_readiness

    DEPLOY_STAGE="web rebuild complete"
    log "=== FRONTEND REBUILD COMPLETE ==="
    log "Web rollback: git revert the offending commit, then re-run $0 --web."
}

# ---------------------------------------------------------------------------
# Readiness check
# ---------------------------------------------------------------------------
wait_for_readiness() {
    local color="$1"
    local start_time=$SECONDS

    while (( SECONDS - start_time < READINESS_TIMEOUT )); do
        # Fail fast if container has exited
        if ! is_container_running "$color"; then
            error "api-${color} has stopped unexpectedly. Check logs:
  docker compose -f $COMPOSE_FILE --env-file $ENV_FILE logs api-${color}"
        fi

        # Check readiness endpoint from inside the container
        if dc exec -T "api-${color}" \
                curl -sf --max-time 3 http://localhost:8080/readiness > /dev/null 2>&1; then
            log "  api-${color} is ready ($((SECONDS - start_time))s)"
            return 0
        fi
        sleep "$READINESS_INTERVAL"
    done

    error "api-${color} did not become ready within ${READINESS_TIMEOUT}s. Aborting.
  The new container is running but Caddy has NOT been switched.
  Investigate: docker compose -f $COMPOSE_FILE --env-file $ENV_FILE logs api-${color}"
}

wait_for_web_readiness() {
    local start_time=$SECONDS

    while (( SECONDS - start_time < 10#$WEB_READINESS_TIMEOUT )); do
        # Distinguish "missing" from "stopped" so the failure mode is debuggable.
        if ! docker inspect kagura-web > /dev/null 2>&1; then
            error "kagura-web container not found. Did 'dc up -d --no-deps --force-recreate web' succeed?"
        fi
        if ! docker inspect --format '{{.State.Running}}' kagura-web | grep -q "true"; then
            error "kagura-web has stopped unexpectedly. Check logs:
  docker compose -f $COMPOSE_FILE --env-file $ENV_FILE logs web"
        fi

        # /api/health is a dedicated 200 endpoint (frontend/src/app/api/health/route.ts).
        # We use `node -e ...` (not curl) because the kagura-web image is
        # node:20-alpine and ships without curl — the Dockerfile HEALTHCHECK
        # uses the same node-based probe, so we ride that contract literally.
        # `timeout 3` (coreutils) bounds the per-iteration wait; node http.get
        # would otherwise inherit the system socket timeout if the server hangs.
        #
        # We call `docker compose ... exec` directly here (NOT via the dc() shell
        # function) because `timeout` uses execvp() and cannot resolve shell
        # functions — `timeout 3 dc ...` would fail with exit 127 "failed to run
        # command 'dc'". Inlining the args keeps --env-file enforced.
        if timeout 3 docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" exec -T web node -e \
                "require('http').get('http://localhost:3000/api/health', (r) => process.exit(r.statusCode === 200 ? 0 : 1)).on('error', () => process.exit(2))" \
                > /dev/null 2>&1; then
            log "  kagura-web is ready ($((SECONDS - start_time))s)"
            return 0
        fi
        sleep "$WEB_READINESS_INTERVAL"
    done

    error "kagura-web did not become ready within ${WEB_READINESS_TIMEOUT}s. Aborting.
  Investigate: docker compose -f $COMPOSE_FILE --env-file $ENV_FILE logs web"
}

# ---------------------------------------------------------------------------
# Caddy config generation
# ---------------------------------------------------------------------------
generate_caddyfile() {
    local upstream="$1"

    if [ ! -f "$CADDYFILE_TPL" ]; then
        error "Caddyfile.tpl not found at $CADDYFILE_TPL"
    fi

    # If a prior `docker compose up` started caddy before ./Caddyfile existed,
    # Docker creates a *directory* at the bind-mount path. cp-ing into a
    # directory would silently leave the mounted config wrong, so fail loudly
    # with recovery steps instead of papering over the bad state.
    if [ -d "$CADDYFILE" ]; then
        error "$CADDYFILE is a directory, not a file.
  Docker created it as a bind-mount placeholder because caddy started before
  ./Caddyfile existed. Stop caddy, remove the directory, and regenerate:
    rm -rf '$CADDYFILE' && $0 --generate-caddyfile"
    fi

    # envsubst with explicit var list — only ${API_UPSTREAM} is replaced.
    # Use tmp + cp (not mv) because Caddyfile is bind-mounted as a single
    # file into the caddy container. mv changes the inode, so the container
    # would keep seeing the old file. cp overwrites in-place, preserving
    # the inode that Docker tracks.
    # shellcheck disable=SC2016  # the single-quoted '${API_UPSTREAM}' is the
    # var-list ARGUMENT to envsubst (it must stay literal so envsubst knows
    # which placeholder to expand) — it is intentionally NOT a shell expansion.
    if ! API_UPSTREAM="$upstream" envsubst '${API_UPSTREAM}' < "$CADDYFILE_TPL" > "$CADDYFILE.tmp"; then
        rm -f "$CADDYFILE.tmp"
        error "envsubst failed — Caddyfile not updated"
    fi
    cp "$CADDYFILE.tmp" "$CADDYFILE"
    rm -f "$CADDYFILE.tmp"
    log "  Caddyfile generated: upstream=$upstream"
}

reload_caddy() {
    # Restart (not just reload) because Docker single-file bind mounts
    # may not reflect host-side cp writes inside the container. A restart
    # re-mounts the file, guaranteeing the new Caddyfile is picked up.
    dc restart caddy
    log "  Caddy restarted"
}

_verify_path_blocked() {
    # Shared edge-block security gate (used by verify_workers_blocked and
    # verify_internal_blocked). Asserts that an internal-only path returns 404
    # via Caddy after every config reload — i.e. it is NOT served through public
    # ingress. Use -k (insecure) because the Cloudflare Origin CA cert is not
    # trusted by the system CA bundle, but we only care about the HTTP status.
    #
    #   $1 label      — human label for logs/errors (e.g. "/internal/*")
    #   $2 probe_path — the absolute path to GET (must yield non-404 if the route
    #                   WERE proxied to the API, so blocked=404 is distinguishable
    #                   from exposed; for /internal we probe the real PUT route
    #                   with GET so an exposed API answers 405, not 404).
    #
    # Domain is extracted from CADDYFILE_TPL so this stays in sync with the
    # configured site block without manual updates here.
    #
    # Retry with backoff for up to ${WORKERS_GATE_TIMEOUT}s to tolerate Caddy's
    # startup window after `dc restart caddy` (a full container restart, not a
    # reload — see reload_caddy). The check is fail-CLOSED: any non-404 result,
    # INCLUDING a connection failure (curl exit != 0 -> empty -> "000"), keeps
    # retrying until the deadline, then aborts loudly. This never weakens the
    # gate — a genuinely misconfigured block (e.g. a stable 200) simply burns
    # the full timeout and then fails, leaving the old color running (#986).
    #
    # The `|| true` is load-bearing: without it the bare command-substitution
    # assignment inherits curl's non-zero exit (7 = connection refused while
    # Caddy is still starting) and `set -e` kills the script *silently* on the
    # first attempt — the original #986 bug, where the retry loop and the
    # ${http_status:-000} fallback below were both dead code.
    local label="$1" probe_path="$2"
    log "  Security gate: verifying ${label} is blocked at Caddy (timeout: ${WORKERS_GATE_TIMEOUT}s)..."
    local domain http_status start_time
    http_status=000
    domain=$(awk '/^[a-zA-Z]/ { gsub(/ *\{.*/, ""); print; exit }' "$CADDYFILE_TPL")
    start_time=$SECONDS
    while (( SECONDS - start_time < 10#$WORKERS_GATE_TIMEOUT )); do
        # --resolve sets both the TCP target AND the TLS SNI to $domain so
        # Caddy selects the correct vhost. A plain -H "Host:" with 127.0.0.1
        # in the URL leaves SNI as "127.0.0.1" and Caddy may not match the
        # site block.
        http_status=$("$CURL" -sk -o /dev/null -w "%{http_code}" \
            --max-time 5 \
            --resolve "${domain}:443:127.0.0.1" \
            "https://${domain}${probe_path}" 2>/dev/null || true)
        http_status=${http_status:-000}
        if [ "$http_status" = "404" ]; then
            log "  ${label} is correctly blocked (HTTP 404, $((SECONDS - start_time))s)"
            return 0
        fi
        sleep "$WORKERS_GATE_INTERVAL"
    done
    error "${label} is NOT blocked by Caddy (HTTP ${http_status}, expected 404) after ${WORKERS_GATE_TIMEOUT}s.
  Aborting before any further steps — the route is exposed and must be fixed before this deploy/rollback can complete.
  Check Caddyfile: the 'handle ${label}' block must appear BEFORE 'handle /api/*' / the catch-all.
  Regenerate and redeploy: $0 --generate-caddyfile && dc restart caddy"
}

verify_workers_blocked() {
    # /api/v1/workers/* carries decrypted connector secrets and is intended for
    # the co-resident ai-worker on the internal Docker network only.
    _verify_path_blocked "/api/v1/workers/*" "/api/v1/workers/config"
}

verify_internal_blocked() {
    # /internal/* is the billing entitlement-push surface (#954): it writes plan
    # tier + addon quota, authenticated only by BILLING_SERVICE_TOKEN. Block it
    # at the edge as defense-in-depth on top of the token. We GET the real PUT
    # route so an *exposed* API answers 405 (method not allowed, route exists),
    # never a 404 — keeping blocked(404) distinguishable from exposed.
    _verify_path_blocked "/internal/*" "/internal/workspaces/probe/plan"
}

cmd_generate_caddyfile() {
    # Bootstrap helper: render ./Caddyfile from Caddyfile.tpl WITHOUT deploying.
    #
    # The rendered Caddyfile is gitignored — Caddyfile.tpl is the single source
    # of truth — so a fresh checkout has no ./Caddyfile. The very first
    # `docker compose up` bind-mounts ./Caddyfile into the caddy container;
    # if the file is absent Docker silently creates a *directory* in its place
    # and caddy fails to load its config. Run this once before that first `up`
    # so Docker mounts a real file. Every subsequent deploy/rollback regenerates
    # it via generate_caddyfile(), so this command is only needed at bootstrap.
    #
    # Upstream tracks the current active color (get_active_color defaults to
    # "blue" when no marker exists, i.e. on first boot), so re-running this on a
    # live host never rewrites the upstream to the wrong color.
    local color
    DEPLOY_STAGE="generate-caddyfile (bootstrap)"
    color="$(get_active_color)"
    log "Generating Caddyfile (bootstrap) for active color: $color"
    generate_caddyfile "api-${color}"
}

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
# Wrapped in main() + guarded by the BASH_SOURCE check so the bats suite can
# `source` this script to unit-test individual functions (e.g.
# verify_workers_blocked) WITHOUT running a real deploy. When executed
# directly, $0 == ${BASH_SOURCE[0]} and main runs exactly as before.
main() {
    cd "$PROJECT_DIR"

    # Final-status line on every exit path (#986). Installed before prerequisite
    # validation so even an early set -e death reports where it aborted.
    trap 'on_exit $?' EXIT

    # Validate prerequisites
    command -v envsubst > /dev/null 2>&1 || error "envsubst not found. Install: apt-get install gettext-base"
    [ -f "$COMPOSE_FILE" ] || error "docker-compose.prod.yml not found at $COMPOSE_FILE"
    [ -f "$ENV_FILE" ] || error ".env.prod not found at $ENV_FILE"
    # Note: `timeout` (coreutils) is also required, but only by --web — it is
    # validated inside cmd_deploy_web so other deploy paths remain usable on
    # environments without it.

    # Validate tunable env vars (timeouts + intervals) are non-negative integers
    [[ "$READINESS_TIMEOUT" =~ ^[0-9]+$ ]] || error "READINESS_TIMEOUT must be an integer (got: $READINESS_TIMEOUT)"
    [[ "$DRAIN_TIMEOUT" =~ ^[0-9]+$ ]] || error "DRAIN_TIMEOUT must be an integer (got: $DRAIN_TIMEOUT)"
    [[ "$WEB_READINESS_TIMEOUT" =~ ^[0-9]+$ ]] || error "WEB_READINESS_TIMEOUT must be an integer (got: $WEB_READINESS_TIMEOUT)"
    [[ "$WEB_READINESS_INTERVAL" =~ ^[1-9][0-9]*$ ]] || error "WEB_READINESS_INTERVAL must be a positive integer >= 1 (got: $WEB_READINESS_INTERVAL); 0 would busy-loop the smoke check"
    [[ "$WORKERS_GATE_TIMEOUT" =~ ^[0-9]+$ ]] || error "WORKERS_GATE_TIMEOUT must be an integer (got: $WORKERS_GATE_TIMEOUT)"
    [[ "$WORKERS_GATE_INTERVAL" =~ ^[1-9][0-9]*$ ]] || error "WORKERS_GATE_INTERVAL must be a positive integer >= 1 (got: $WORKERS_GATE_INTERVAL); 0 would busy-loop the security gate"

    # Ensure marker directory exists
    mkdir -p "$(dirname "$MARKER_FILE")"

    case "${1:-}" in
        --rollback)
            cmd_rollback
            ;;
        --status)
            DEPLOY_STAGE="readonly"
            cmd_status
            ;;
        --web)
            cmd_deploy_web
            ;;
        --generate-caddyfile)
            cmd_generate_caddyfile
            ;;
        --help|-h)
            DEPLOY_STAGE="readonly"
            echo "Usage: $0 [--rollback|--status|--web|--generate-caddyfile|--help]"
            echo ""
            echo "  (no args)             Deploy to the inactive API color (zero-downtime blue-green)"
            echo "  --rollback            Switch back to the previous API color"
            echo "  --status              Show current active API color and container states"
            echo "  --web                 Rebuild + restart kagura-web in place"
            echo "  --generate-caddyfile  Render ./Caddyfile from Caddyfile.tpl (bootstrap; no deploy)"
            echo ""
            echo "Environment variables:"
            echo "  READINESS_TIMEOUT      Seconds to wait for API /readiness (default: 60)"
            echo "  DRAIN_TIMEOUT          Seconds to drain old API container (default: 30)"
            echo "  WEB_READINESS_TIMEOUT  Seconds to wait for web /api/health (default: 30)"
            echo "  WEB_READINESS_INTERVAL Seconds between web health checks (default: 2)"
            echo "  WORKERS_GATE_TIMEOUT   Seconds to wait for an edge-blocked path (/api/v1/workers/*, /internal/*) to 404 at Caddy (default: 30)"
            echo "  WORKERS_GATE_INTERVAL  Seconds between security-gate checks; shared by the workers + internal gates (default: 2)"
            ;;
        "")
            cmd_deploy
            ;;
        *)
            error "Unknown argument: $1 (try --help)"
            ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
