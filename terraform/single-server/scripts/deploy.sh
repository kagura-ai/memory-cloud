#!/usr/bin/env bash
# =============================================================================
# Kagura Memory Cloud — Blue-Green Deploy Script (Issue #239)
# =============================================================================
# Zero-downtime deployment for the API container. Switches between api-blue
# and api-green while keeping Caddy routing live.
#
# Usage:
#   ./scripts/deploy.sh              # normal deploy
#   ./scripts/deploy.sh --rollback   # switch back to previous color
#   ./scripts/deploy.sh --status     # show current active color
#
# Requires: docker compose, curl, envsubst (gettext-base)
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$PROJECT_DIR/docker-compose.prod.yml"
CADDYFILE_TPL="$PROJECT_DIR/Caddyfile.tpl"
CADDYFILE="$PROJECT_DIR/Caddyfile"
MARKER_FILE="/opt/kagura-memory/active-color"
ENV_FILE="$PROJECT_DIR/.env.prod"

READINESS_TIMEOUT="${READINESS_TIMEOUT:-60}"   # seconds to wait for /readiness
READINESS_INTERVAL=2                            # seconds between checks
DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-30}"            # seconds to drain old container

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()   { echo "[deploy] $(date -u +%H:%M:%S) $*"; }
error() { echo "[deploy] ERROR: $*" >&2; exit 1; }

dc() { docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }

get_active_color() {
    local color
    color="$(cat "$MARKER_FILE" 2>/dev/null || echo "blue")"
    # Validate — must be exactly "blue" or "green"
    if [ "$color" != "blue" ] && [ "$color" != "green" ]; then
        error "Marker file $MARKER_FILE contains invalid value: '$color'. Expected 'blue' or 'green'."
    fi
    echo "$color"
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

    # Show container states
    dc ps --format "table {{.Name}}\t{{.Status}}" 2>/dev/null \
        | grep -E "kagura-api|NAME" || true
}

cmd_rollback() {
    log "=== ROLLBACK ==="
    local active inactive
    active="$(get_active_color)"
    inactive="$(get_inactive_color)"

    # Check that the previous color's container is still running
    if ! is_container_running "$inactive"; then
        error "api-${inactive} is not running — cannot rollback. Start it first:
  docker compose -f $COMPOSE_FILE --env-file $ENV_FILE up -d api-${inactive}"
    fi

    # Write marker BEFORE switching Caddy (atomic: same filesystem mv)
    log "Switching Caddy: api-${active} -> api-${inactive}"
    echo "$inactive" > "${MARKER_FILE}.tmp"
    mv "${MARKER_FILE}.tmp" "$MARKER_FILE"
    generate_caddyfile "api-${inactive}"
    reload_caddy

    log "Rollback complete. Active: $inactive"
}

cmd_deploy() {
    log "=== BLUE-GREEN DEPLOY ==="
    local active inactive
    active="$(get_active_color)"
    inactive="$(get_inactive_color)"
    log "Current active: $active — deploying to: $inactive"

    # Step 1: Build new image
    log "Step 1/7: Building api-${inactive} image..."
    dc build "api-${inactive}"

    # Step 2: Start the inactive color (uses new image)
    # Re-enable restart policy in case it was disabled by a previous deploy.
    log "Step 2/7: Starting api-${inactive}..."
    dc up -d "api-${inactive}"
    docker update --restart=always "kagura-api-${inactive}" 2>/dev/null || true

    # Step 3: Wait for readiness (DB + Qdrant + Redis all reachable)
    log "Step 3/7: Waiting for api-${inactive} readiness (timeout: ${READINESS_TIMEOUT}s)..."
    wait_for_readiness "$inactive"

    # Step 4: Run Alembic migrations on the NEW container
    # Forward-compatible by convention: both old and new code work with new schema.
    log "Step 4/7: Running database migrations on api-${inactive}..."
    dc exec -T "api-${inactive}" alembic upgrade head

    # Step 5: Write marker BEFORE switching Caddy (crash-safe ordering)
    log "Step 5/7: Updating marker -> ${inactive}"
    echo "$inactive" > "${MARKER_FILE}.tmp"
    mv "${MARKER_FILE}.tmp" "$MARKER_FILE"

    # Step 6: Switch Caddy upstream
    log "Step 6/7: Switching Caddy upstream -> api-${inactive}..."
    generate_caddyfile "api-${inactive}"
    reload_caddy

    # Step 7: Drain and stop old container
    # Disable restart policy first — otherwise `restart: always` revives
    # the container immediately after stop.
    log "Step 7/7: Draining api-${active} for ${DRAIN_TIMEOUT}s..."
    sleep "$DRAIN_TIMEOUT"
    docker update --restart=no "kagura-api-${active}" 2>/dev/null || true
    dc stop "api-${active}" || true
    log "api-${active} stopped (restart policy disabled)."

    log "=== DEPLOY COMPLETE ==="
    log "Active: $inactive"
    log "To rollback: $0 --rollback"
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

# ---------------------------------------------------------------------------
# Caddy config generation
# ---------------------------------------------------------------------------
generate_caddyfile() {
    local upstream="$1"

    if [ ! -f "$CADDYFILE_TPL" ]; then
        error "Caddyfile.tpl not found at $CADDYFILE_TPL"
    fi

    # envsubst with explicit var list — only ${API_UPSTREAM} is replaced
    if ! API_UPSTREAM="$upstream" envsubst '${API_UPSTREAM}' < "$CADDYFILE_TPL" > "$CADDYFILE.tmp"; then
        rm -f "$CADDYFILE.tmp"
        error "envsubst failed — Caddyfile not updated"
    fi
    mv "$CADDYFILE.tmp" "$CADDYFILE"
    log "  Caddyfile generated: upstream=$upstream"
}

reload_caddy() {
    dc exec -T caddy caddy reload --config /etc/caddy/Caddyfile --force
    log "  Caddy reloaded"
}

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
cd "$PROJECT_DIR"

# Validate prerequisites
command -v envsubst > /dev/null 2>&1 || error "envsubst not found. Install: apt-get install gettext-base"
[ -f "$COMPOSE_FILE" ] || error "docker-compose.prod.yml not found at $COMPOSE_FILE"
[ -f "$ENV_FILE" ] || error ".env.prod not found at $ENV_FILE"

# Validate timeout values are integers
[[ "$READINESS_TIMEOUT" =~ ^[0-9]+$ ]] || error "READINESS_TIMEOUT must be an integer (got: $READINESS_TIMEOUT)"
[[ "$DRAIN_TIMEOUT" =~ ^[0-9]+$ ]] || error "DRAIN_TIMEOUT must be an integer (got: $DRAIN_TIMEOUT)"

# Ensure marker directory exists
mkdir -p "$(dirname "$MARKER_FILE")"

case "${1:-}" in
    --rollback)
        cmd_rollback
        ;;
    --status)
        cmd_status
        ;;
    --help|-h)
        echo "Usage: $0 [--rollback|--status|--help]"
        echo ""
        echo "  (no args)    Deploy to the inactive color (zero-downtime)"
        echo "  --rollback   Switch back to the previous color"
        echo "  --status     Show current active color and container states"
        echo ""
        echo "Environment variables:"
        echo "  READINESS_TIMEOUT  Seconds to wait for /readiness (default: 60)"
        echo "  DRAIN_TIMEOUT      Seconds to drain old container (default: 30)"
        ;;
    "")
        cmd_deploy
        ;;
    *)
        error "Unknown argument: $1 (try --help)"
        ;;
esac
