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

# docker indirection, same pattern as CURL above: only the bridge-restart step
# goes through "$DOCKER" so the bats suite can stub it
# (tests/deploy_bridge_restart.bats). Every other docker call in this file
# either runs through dc() or belongs to a flow the suite never exercises.
DOCKER="${DOCKER:-docker}"

# The chat-bridge worker is NOT a service in this repo's docker-compose.prod.yml
# — it is a separate co-resident stack sharing this VM and docker network. That
# is why the restart below uses plain `docker` and not dc(): compose would not
# know the container. Overridable so a host that names it differently (or a test)
# can point at the right one (#1476).
BRIDGE_WORKER_CONTAINER="${BRIDGE_WORKER_CONTAINER:-kagura-bridge-worker-1}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()   { echo "[deploy] $(date -u +%H:%M:%S) $*"; }
error() { echo "[deploy] ERROR: $*" >&2; exit 1; }

dc() { docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }

# Tracks the current step so the EXIT trap can report *where* a run aborted.
# Updated at the head of each step; read only by on_exit().
DEPLOY_STAGE="startup"

# Set to 1 when the bridge step could not be PROVEN good (#1480). The deploy
# still succeeds — the API cutover is independent and already done — but the
# summary must not read as clean. Deliberately not an abort: failing a completed
# cutover because a co-resident stack is unhappy would invite a needless
# rollback, which is how #1476's manual step got skipped in the first place.
BRIDGE_STEP_DEGRADED=0

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
       color serving traffic right now. Find it, then write it back:
           docker ps --format '{{.Names}}' | grep kagura-api-
           echo blue  > $MARKER_FILE
           echo green > $MARKER_FILE"
    fi
    # -s says non-empty, not readable: a root-owned or IO-failing marker passes
    # that check and then dies inside the command substitution, where `set -e`
    # aborts with no message of ours (Copilot review on #1462). An operator
    # mid-incident needs the reason, so catch the read explicitly.
    local color
    if ! color="$(cat "$MARKER_FILE" 2>/dev/null)"; then
        error "Marker file $MARKER_FILE exists but could not be read.
       Check ownership and permissions:
           ls -l $MARKER_FILE"
    fi
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
# would build the color that IS live and drain the one that is not. Downstream
# consumers that implement a connect-level failover to the other color then mask
# the mismatch instead of surfacing it: the failover carries the traffic, so the
# system keeps looking healthy while a safety net quietly serves as the normal
# path and its warnings drown out every other signal.
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

# Publish a new active color WITHOUT changing the marker file's inode (#1480).
#
# This file is bind-mounted into co-resident stacks as a SINGLE FILE
# (a co-resident consumer mounts it read-only to follow the active color). A single-file
# bind mount resolves to an inode at container start: replacing the file with
# `mv` publishes a NEW inode, and the running container keeps reading the OLD
# one forever. It sees the color it booted with, no matter how many times this
# script rewrites the marker.
#
# That is not a hypothesis. Measured on docker 29.3.1 with a single-file bind
# mount of this exact shape:
#   mv  + container left running -> container still reads the OLD color
#   mv  + `docker restart`       -> container reads the new color
#   cp  + container left running -> container reads the new color, NO restart
#
# So `mv` is what forced that consumer onto a static internal-URL pin, which
# in turn is what made deploy Step 6b's `docker restart` unable to re-point it
# (a restart cannot re-read an .env). Writing in place fixes it at the source.
#
# generate_caddyfile() 160 lines below already made exactly this trade for the
# Caddyfile, for verbatim this reason — see its `tmp + cp (not mv)` comment.
# This is that same fix, applied to the file that needed it more.
#
# The cost, stated plainly: `cp` is not atomic, so a crash mid-write can leave
# the marker truncated where `mv` could not. That is deliberate and is the
# behaviour #1448 asked for — get_active_color() treats an empty or malformed
# marker as a loud, recoverable error with recovery steps, never as a guess.
# the consumer's reader fails closed the same way. A single writer (this
# script), 6 bytes, versus a defect that silently degraded production for 65
# hours: in-place wins.
write_marker() {
    local color="$1"

    # Remember what was published, so a half-finished write can be reported
    # accurately — and undone. `cp` opens the destination with O_TRUNC, so a
    # failure part-way through (ENOSPC, EIO, a read-only remount) leaves the
    # LIVE marker truncated. `mv` could not do that; this is the real cost of
    # preserving the inode, and it is handled rather than hand-waved.
    #
    # Read with `read`, not `cat ... || fallback`: #1448's guard bans that shape
    # outright because it is how a silent default color creeps back in, and it
    # is exactly the shape one reaches for here. `read` also avoids a subshell.
    local previous=""
    if [ -s "$MARKER_FILE" ]; then
        IFS= read -r previous < "$MARKER_FILE" 2>/dev/null || previous=""
    fi

    # Stage first so a failed write never truncates the live marker, then copy
    # ONTO the original inode rather than renaming over it.
    if ! echo "$color" > "${MARKER_FILE}.tmp"; then
        rm -f "${MARKER_FILE}.tmp"
        error "Could not stage marker at ${MARKER_FILE}.tmp — check disk and permissions.
       The live marker was NOT touched."
    fi
    if ! cp "${MARKER_FILE}.tmp" "$MARKER_FILE"; then
        rm -f "${MARKER_FILE}.tmp"
        # Best-effort restore. It may itself fail on a full or read-only
        # filesystem, so the message must not promise it worked.
        if [ -n "$previous" ]; then
            echo "$previous" > "$MARKER_FILE" 2>/dev/null || true
        fi
        error "Could not write marker $MARKER_FILE, and it may now be TRUNCATED.
       Caddy has NOT been switched, so traffic is still on '${previous:-unknown}'.
       Restoring '${previous:-unknown}' was attempted — verify and fix by hand:
           cat $MARKER_FILE
           echo ${previous:-blue} > $MARKER_FILE"
    fi
    rm -f "${MARKER_FILE}.tmp"

    # Read back before claiming success. `cp file dir` SUCCEEDS by copying into
    # the directory, so a marker path that is somehow a directory would leave
    # the published color unchanged while cp exits 0 — the same "reported the
    # action, not the outcome" mistake this issue is about.
    #
    # Read through get_active_color rather than cat: it is the same validated
    # reader every other consumer uses, so this cannot accidentally accept a
    # value the rest of the script would reject. It also keeps this function
    # clear of the `cat ... || fallback` shape that #1448's guard forbids.
    local published
    published="$(get_active_color)"
    if [ "$published" != "$color" ]; then
        error "Marker $MARKER_FILE reads back as '$published', not '$color'.
       Traffic may already be switched — write it by hand to match reality:
           echo $color > $MARKER_FILE"
    fi
}

is_container_running() {
    local container="kagura-api-$1"
    docker inspect --format '{{.State.Running}}' "$container" 2>/dev/null | grep -q "true"
}

# Is the co-resident bridge worker running on this host right now?
#
#   0 — running
#   1 — definitively not running (docker answered, the name was not in the list)
#   2 — could not be determined (the docker query itself failed)
#
# 1 and 2 are kept apart deliberately. Collapsing them would let an unreachable
# or permission-denied docker daemon read as "no bridge on this host", so the
# deploy would print a benign skip and move on having never attempted the
# restart — a silent absorption of exactly the kind this whole step exists to
# stop.
#
# Deliberately NOT `docker ps ... | grep -qx`: `grep -q` exits at the first
# match and can SIGPIPE `docker ps`, which under `set -o pipefail` surfaces as a
# failed pipeline — i.e. a *running* container intermittently reported as
# absent. That is the exact shape of the #986 silent-death bug. Capture first,
# then compare whole lines, so no pipeline status is involved. Whole-line
# equality also means a container named `kagura-bridge-worker-10` never
# satisfies the check for `kagura-bridge-worker-1`.
bridge_worker_is_running() {
    local names name
    # `|| return 2` also exempts the assignment from `set -e`.
    names="$("$DOCKER" ps --format '{{.Names}}' 2>/dev/null)" || return 2
    while IFS= read -r name; do
        # An empty `names` still feeds the loop one empty line, so guard -n
        # rather than letting "" match an empty container name.
        if [ -n "$name" ] && [ "$name" = "$BRIDGE_WORKER_CONTAINER" ]; then
            return 0
        fi
    done <<< "$names"
    return 1
}

# Print the worker's STATIC upstream pin, or nothing when it has none.
#
# Empty output means the variable is absent or set-but-empty — in both cases the
# worker resolves the color from the bind-mounted marker instead, which is the
# configuration we want. Returns non-zero only when docker could not be asked.
bridge_pinned_url() {
    local env_lines line
    env_lines="$("$DOCKER" inspect "$BRIDGE_WORKER_CONTAINER" \
        --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null)" || return 1
    while IFS= read -r line; do
        case "$line" in
            KMC_INTERNAL_URL=*)
                printf '%s' "${line#KMC_INTERNAL_URL=}"
                return 0
                ;;
        esac
    done <<< "$env_lines"
    return 0
}

# Has the worker fallen back to the other color since the deploy touched it?
#
# The consumer logs `control_plane.cross_color_fallback` when its primary
# upstream refuses a connection and it retries the other color. That line is the
# ONLY reliable outward sign of a stranded worker: its /health keeps returning
# {"status":"ok"} the whole time, which is why the condition once ran 65 hours
# unnoticed. Treat it as a deploy signal, not log noise (#1480).
bridge_fallback_count() {
    local since="${1:-5m}" logs
    logs="$("$DOCKER" logs --since "$since" "$BRIDGE_WORKER_CONTAINER" 2>&1)" || return 1
    # grep -c exits 1 on zero matches; `|| true` keeps that out of `set -e`.
    printf '%s\n' "$logs" | grep -c 'control_plane.cross_color_fallback' || true
}

# Extract the host from a URL, with no port and no path: http://api-blue:8080
# -> api-blue. Used so the color check is an EXACT host comparison.
#
# A substring test (`case $pin in *api-blue*`) reports a pin of
# `http://api-blue2:8080` as a match for api-blue, which is a false clean —
# precisely the failure class this file exists to remove.
url_host() {
    local url="${1:-}" host
    host="${url#*://}"   # strip scheme, if any
    host="${host%%/*}"   # strip path
    host="${host%%\?*}"  # strip query
    host="${host%%:*}"   # strip port
    printf '%s' "$host"
}

# Check the worker's CONFIGURED upstream. Usable the instant `docker restart`
# returns, because a container's env is static — it does not wait for the worker
# to finish booting.
verify_bridge_pin() {
    local color="${1:-}" pin

    # An empty color would make every comparison below trivially permissive,
    # "verifying" a worker against nothing. Refuse rather than pass.
    if [ -z "$color" ]; then
        log "WARNING: verify_bridge_pin called with no color — nothing verified."
        return 1
    fi

    if ! pin="$(bridge_pinned_url)"; then
        log "  Could not read ${BRIDGE_WORKER_CONTAINER} config — upstream NOT verified."
        return 1
    fi

    if [ -n "$pin" ]; then
        case "$(url_host "$pin")" in
            "api-${color}")
                log "  ${BRIDGE_WORKER_CONTAINER} upstream pin is ${pin} — matches api-${color}."
                ;;
            *)
                log "WARNING: ${BRIDGE_WORKER_CONTAINER} is pinned to '${pin}', NOT api-${color}."
                log "         A restart CANNOT fix this — the pin is baked into the container"
                log "         from deploy/.env, and 'docker restart' does not re-read that file."
                log "         Chat ingest is now riding the cross-color fallback (#1480)."
                log "         Re-point and RECREATE (not restart):"
                log "             sudo sed -i 's|^KMC_INTERNAL_URL=.*|KMC_INTERNAL_URL=http://api-${color}:8080|' \\"
                log "                 /opt/kagura-bridge/src/deploy/.env"
                log "             cd /opt/kagura-bridge/src && docker compose -f deploy/compose.yml \\"
                log "                 --env-file deploy/.env up -d --no-deps --force-recreate worker"
                log "         Better: blank KMC_INTERNAL_URL so the worker follows the marker,"
                log "         which this script now writes in place (#1480)."
                return 1
                ;;
        esac
    else
        # Honest scope: with no pin the worker resolves from the marker, and
        # this script cannot read what it resolved TO. The fallback-log check
        # below is the only outward evidence, so say that rather than claim the
        # upstream was confirmed.
        log "  ${BRIDGE_WORKER_CONTAINER} has no static pin — it follows the marker (now ${color})."
        log "  (Resolved upstream is not readable from here; the fallback check is the evidence.)"
    fi
    return 0
}

# Check the worker's OWN verdict: has it fallen back to the other color?
#
# TIMING IS LOAD-BEARING. Run this only once the old color is STOPPED. Before
# the drain, a stranded worker can still reach the color it is pinned to, so it
# has no reason to log a fallback and this check cannot fire — it would report
# clean by construction, which is the very defect #1480 is about. After Step 7
# the old color is gone, so a stranded worker surfaces immediately.
verify_bridge_fallback() {
    local window="${1:-5m}" fallbacks
    if ! fallbacks="$(bridge_fallback_count "$window")"; then
        # "Could not ask" is not "fine" — the same distinction bridge presence
        # detection already makes. A silent skip here would be a false clean.
        log "WARNING: could not read ${BRIDGE_WORKER_CONTAINER} logs — fallback state UNKNOWN."
        log "         Treating as not verified rather than assuming healthy."
        return 1
    fi
    if [ "${fallbacks:-0}" -gt 0 ]; then
        log "WARNING: ${BRIDGE_WORKER_CONTAINER} logged ${fallbacks} cross-color fallback event(s)"
        log "         in the last ${window}. It is reaching the API only via the safety net,"
        log "         which means it is NOT on the live color."
        log "         Inspect: docker logs --since 30m ${BRIDGE_WORKER_CONTAINER} | grep cross_color_fallback"
        return 1
    fi
    return 0
}

# Both halves, for callers that are not mid-deploy (--verify-bridge), where the
# old color is already stopped and the fallback signal is meaningful.
verify_bridge_upstream() {
    local color="${1:-}" window="${2:-5m}" ok=0
    verify_bridge_pin "$color" || ok=1
    verify_bridge_fallback "$window" || ok=1
    return "$ok"
}

# Restart the chat-bridge worker after the active API color changes (#1476).
#
# The worker resolves the API container by color when it connects and then holds
# those connections. A flip leaves it talking to the color this script is about
# to drain. If it implements a connect-level failover to the other color it stays
# up — but on the safety net rather than the normal path, which has twice masked
# a prolonged outage here instead of surfacing it.
#
# Nothing else in the system knows a flip happened, so this is the only place
# the restart can be triggered from.
#
# Call it AFTER Caddy has been switched: restarting while the old color still
# serves would just re-pin the worker to the color being drained.
#
# $1 is the color that is now live. It is REQUIRED: #1480 was possible because
# this step reported "restarted." without ever checking where the worker ended
# up. A restart is the action; landing on $1 is the outcome, and only the
# outcome is worth reporting.
restart_bridge_worker() {
    local new_color="$1"
    # `|| presence=$?` keeps a non-zero return from killing the run under `set -e`.
    local presence=0
    bridge_worker_is_running || presence=$?

    # Presence-gated: deploy.sh is also the OSS single-server script, and a host
    # without the bridge stack must log a skip, not an error.
    if [ "$presence" -eq 1 ]; then
        log "  ${BRIDGE_WORKER_CONTAINER} is not running on this host — skipping bridge restart."
        return 0
    fi

    # presence == 2: docker could not be queried. Try the restart anyway rather
    # than skipping — if docker is genuinely down the restart fails and drops
    # into the loud warning below, which is the honest outcome; if only the
    # query was broken, the restart still does its job.
    if [ "$presence" -ne 0 ]; then
        log "WARNING: could not query docker to check for ${BRIDGE_WORKER_CONTAINER}."
        log "         Attempting the restart anyway rather than silently skipping it."
    fi

    log "  Restarting ${BRIDGE_WORKER_CONTAINER} (active API color changed)..."
    if "$DOCKER" restart "$BRIDGE_WORKER_CONTAINER" > /dev/null 2>&1; then
        log "  ${BRIDGE_WORKER_CONTAINER} restarted."
        # The restart happened. That is NOT the same as the worker now talking
        # to the new color — the whole of #1480. Check the configured upstream
        # now; the worker's own fallback verdict is only meaningful after the
        # old color is drained, so that half runs at the end of Step 7.
        if ! verify_bridge_pin "$new_color"; then
            BRIDGE_STEP_DEGRADED=1
        fi
        return 0
    fi

    # Non-fatal on purpose. By this point the API cutover has completed and the
    # new color is serving; a bridge that will not come back is a bridge
    # problem. Aborting here would report a healthy deploy as failed and invite
    # an unnecessary rollback. Say it loudly instead.
    BRIDGE_STEP_DEGRADED=1
    log "WARNING: ${BRIDGE_WORKER_CONTAINER} failed to restart. Chat ingest may still be"
    log "         pinned to the drained API color. Restart it manually:"
    log "             docker restart ${BRIDGE_WORKER_CONTAINER}"
    return 0
}

# Print the closing bridge verdict. Called from the deploy/rollback summary so
# a degraded bridge is the LAST thing an operator reads, not a line scrolled off
# the top by Step 7's drain (#1480).
report_bridge_state() {
    if [ "${BRIDGE_STEP_DEGRADED:-0}" -eq 0 ]; then
        return 0
    fi
    log ""
    log "*** BRIDGE NOT VERIFIED — the API cutover succeeded, chat ingest did not. ***"
    log "    The API is serving normally. The bridge worker is NOT confirmed to be"
    log "    on the new color; see the WARNING above for the exact remediation."
    log "    Re-check at any time with: $0 --verify-bridge"
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

    # Write marker BEFORE switching Caddy. In place, so co-resident readers that
    # bind-mount it as a single file actually see the change (#1480).
    log "Switching Caddy: api-${active} -> api-${inactive}"
    write_marker "$inactive"
    DEPLOY_STAGE="rollback: switch Caddy -> api-${inactive} (incl. security gate)"
    generate_caddyfile "api-${inactive}"
    reload_caddy
    verify_workers_blocked
    verify_internal_blocked

    # A rollback is a color flip too, so the bridge worker is left pointing at
    # the color we just switched away from — same failure as an ordinary deploy
    # (#1476). The issue only named cmd_deploy; this path needs it just as much.
    DEPLOY_STAGE="rollback: restart bridge worker"
    restart_bridge_worker "$inactive"

    DEPLOY_STAGE="rollback complete (active: ${inactive})"
    log "Rollback complete. Active: $inactive"
    report_bridge_state
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

    # Step 5: Write marker BEFORE switching Caddy (crash-safe ordering), and
    # write it IN PLACE so single-file bind-mount readers see it (#1480).
    DEPLOY_STAGE="Step 5/7: update marker -> ${inactive}"
    log "Step 5/7: Updating marker -> ${inactive}"
    write_marker "$inactive"

    # Step 6: Switch Caddy upstream
    DEPLOY_STAGE="Step 6/7: switch Caddy upstream -> api-${inactive} (incl. security gate)"
    log "Step 6/7: Switching Caddy upstream -> api-${inactive}..."
    generate_caddyfile "api-${inactive}"
    reload_caddy
    verify_workers_blocked
    verify_internal_blocked

    # Step 6b: Restart the co-resident bridge worker now that the color moved.
    # Numbered 6b rather than renumbering to /8 because it rides on Step 6's
    # switch — it is only correct once Caddy points at the new color.
    DEPLOY_STAGE="Step 6b/7: restart ${BRIDGE_WORKER_CONTAINER}"
    log "Step 6b/7: Restarting the bridge worker after the color switch..."
    restart_bridge_worker "$inactive"

    # Step 7: Drain and stop old container
    # Disable restart policy first — otherwise `restart: always` revives
    # the container immediately after stop.
    DEPLOY_STAGE="Step 7/7: drain + stop api-${active}"
    log "Step 7/7: Draining api-${active} for ${DRAIN_TIMEOUT}s..."
    sleep "$DRAIN_TIMEOUT"
    docker update --restart=no "kagura-api-${active}" 2>/dev/null || true
    dc stop "api-${active}" || true
    log "api-${active} stopped (restart policy disabled)."

    # NOW the fallback signal means something: api-${active} is gone, so a
    # worker still pointed at it must fall back, and that is observable. Running
    # this at Step 6b instead would report clean by construction, because the
    # old color was still answering (#1480).
    if bridge_worker_is_running; then
        DEPLOY_STAGE="Step 7/7: verify ${BRIDGE_WORKER_CONTAINER} after drain"
        log "  Checking ${BRIDGE_WORKER_CONTAINER} now that api-${active} is stopped..."
        if ! verify_bridge_fallback "${BRIDGE_FALLBACK_WINDOW:-5m}"; then
            BRIDGE_STEP_DEGRADED=1
        fi
    fi

    DEPLOY_STAGE="deploy complete (active: ${inactive})"
    log "=== DEPLOY COMPLETE ==="
    log "Active: $inactive"
    log "To rollback: $0 --rollback"
    report_bridge_state
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

cmd_verify_bridge() {
    # Read-only. Exits non-zero when the bridge worker is not provably on the
    # active color, so cron/monitoring can consume it — this is the "somewhere"
    # that criterion 3 of #1480 asks for. A non-zero exit is safe HERE precisely
    # because nothing is mid-flight; inside a deploy the same condition only
    # warns, because the API cutover has already succeeded by then.
    local active
    active="$(get_active_color)"
    log "Verifying ${BRIDGE_WORKER_CONTAINER} against active color: ${active}"

    local presence=0
    bridge_worker_is_running || presence=$?
    if [ "$presence" -eq 1 ]; then
        log "  ${BRIDGE_WORKER_CONTAINER} is not running on this host — nothing to verify."
        return 0
    fi
    if [ "$presence" -ne 0 ]; then
        error "Could not query docker for ${BRIDGE_WORKER_CONTAINER}."
    fi

    # Wider log window than the deploy path: this runs at an arbitrary time, so
    # a fallback that started half an hour ago is exactly what it should catch.
    # Mid-deploy the worker was restarted seconds earlier, where 5m is right.
    if verify_bridge_upstream "$active" "${BRIDGE_FALLBACK_WINDOW:-30m}"; then
        log "Bridge upstream verified: api-${active}"
        return 0
    fi
    error "Bridge worker is NOT verified against api-${active} — see the warning above."
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
        --verify-bridge)
            DEPLOY_STAGE="verify bridge upstream"
            cmd_verify_bridge
            ;;
        --help|-h)
            DEPLOY_STAGE="readonly"
            echo "Usage: $0 [--rollback|--status|--web|--verify-bridge|--generate-caddyfile|--help]"
            echo ""
            echo "  (no args)             Deploy to the inactive API color (zero-downtime blue-green)"
            echo "  --rollback            Switch back to the previous API color"
            echo "  --status              Show current active API color and container states"
            echo "  --web                 Rebuild + restart kagura-web in place"
            echo "  --verify-bridge       Check the bridge worker is on the active color; exits non-zero if not"
            echo "  --generate-caddyfile  Render ./Caddyfile from Caddyfile.tpl (bootstrap; no deploy)"
            echo ""
            echo "Environment variables:"
            echo "  READINESS_TIMEOUT        Seconds to wait for API /readiness (default: 60)"
            echo "  DRAIN_TIMEOUT            Seconds to drain old API container (default: 30)"
            echo "  WEB_READINESS_TIMEOUT    Seconds to wait for web /api/health (default: 30)"
            echo "  WEB_READINESS_INTERVAL   Seconds between web health checks (default: 2)"
            echo "  WORKERS_GATE_TIMEOUT     Seconds to wait for an edge-blocked path (/api/v1/workers/*, /internal/*) to 404 at Caddy (default: 30)"
            echo "  WORKERS_GATE_INTERVAL    Seconds between security-gate checks; shared by the workers + internal gates (default: 2)"
            echo "  BRIDGE_WORKER_CONTAINER  Co-resident chat-bridge worker restarted after a color flip; skipped when absent (default: kagura-bridge-worker-1)"
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
