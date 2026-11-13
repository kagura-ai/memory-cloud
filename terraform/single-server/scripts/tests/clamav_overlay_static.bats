#!/usr/bin/env bats
# =============================================================================
# Static guard for the optional clamd of the upload malware scan (#565).
#
# The application cannot read clamd's configuration over the socket, so the
# properties the scan relies on are pinned where they are shipped:
#
#   - clamd.conf: `AlertExceedsMax yes` (without it clamd answers "OK" for a
#     file past one of its limits, i.e. content it never scanned), and
#     StreamMaxLength / MaxFileSize at least the API's default
#     CLAMD_STREAM_MAX_BYTES (otherwise clamd refuses, or skips, files the API
#     accepts).
#   - the production overlay publishes no port (clamd's TCP protocol has no
#     authentication), caps memory, drops capabilities, pins the image by
#     digest, and patches no API service (CLAMD_* come from .env.prod, which
#     deploy.sh's single-file recreates keep).
#   - the dev profile uses the same image and the same clamd.conf.
#
# grep/awk only: the CI shell job installs bats and nothing else.
# compose_port_bind.bats separately keeps a published 3310 off 0.0.0.0, and
# log_hygiene_static.bats pins the overlay's log rotation.
# =============================================================================

PROJECT_DIR="$BATS_TEST_DIRNAME/../.."
REPO_ROOT="$PROJECT_DIR/../.."
OVERLAY="$PROJECT_DIR/docker-compose.clamav.yml"
CONF="$PROJECT_DIR/clamav/clamd.conf"
DEV_COMPOSE="$REPO_ROOT/docker-compose.yml"
SETTINGS="$REPO_ROOT/backend/src/config/settings.py"

# CLAMD_STREAM_MAX_BYTES default (100 MiB); the last test ties it to settings.py.
STREAM_MAX_DEFAULT=104857600

# Value of one clamd.conf directive (the last active line wins, as in clamd).
conf_value() {   # $1 directive
    awk -v key="$1" '$1 == key { v = $2 } END { print v }' "$CONF"
}

# clamd size syntax (plain bytes, or a K/M suffix) to bytes.
to_bytes() {   # $1 value
    local v="$1"
    case "$v" in
        *[Mm]) echo $(( ${v%?} * 1024 * 1024 )) ;;
        *[Kk]) echo $(( ${v%?} * 1024 )) ;;
        *)     echo "$v" ;;
    esac
}

# Names of the entries under the overlay's top-level `services:`.
overlay_services() {
    awk '
        /^services:/ { inside = 1; next }
        inside && /^[^[:space:]#]/ { exit }
        inside && /^  [A-Za-z0-9_-]+:[[:space:]]*$/ { name = $1; sub(/:$/, "", name); print name }
    ' "$OVERLAY"
}

# Body of one service of a compose file (4-space indented lines).
service_block() {   # $1 file, $2 service
    awk -v svc="$2" '
        $0 ~ "^  " svc ":[[:space:]]*$" { inside = 1; next }
        inside && /^  [^[:space:]#]/ { exit }
        inside && /^[^[:space:]#]/ { exit }
        inside { print }
    ' "$1"
}

@test "clamd.conf: AlertExceedsMax is on" {
    [ "$(conf_value AlertExceedsMax)" = "yes" ]
}

@test "clamd.conf: StreamMaxLength and MaxFileSize cover CLAMD_STREAM_MAX_BYTES" {
    for key in StreamMaxLength MaxFileSize; do
        v="$(conf_value "$key")"
        [ -n "$v" ] || { echo "$key not set"; return 1; }
        [ "$(to_bytes "$v")" -ge "$STREAM_MAX_DEFAULT" ] \
            || { echo "$key $v is below $STREAM_MAX_DEFAULT bytes"; return 1; }
    done
}

@test "clamd.conf: TCP 3310 for the API and the local socket the image's entrypoint waits for" {
    [ "$(conf_value TCPSocket)" = "3310" ]
    [ -n "$(conf_value LocalSocket)" ]
    [ "$(conf_value User)" = "clamav" ]
    # A TCPAddr would have to be the container's own address; none is shipped.
    run grep -cE '^[[:space:]]*TCPAddr[[:space:]]' "$CONF"
    [ "$output" -eq 0 ]
}

@test "overlay: one service, clamav, and no patch of an API service" {
    run overlay_services
    [ "$status" -eq 0 ]
    [ "$output" = "clamav" ]
}

@test "overlay: no published port" {
    run grep -cE '^[[:space:]]*(ports|expose):' "$OVERLAY"
    [ "$output" -eq 0 ]
}

@test "overlay: memory cap, no-new-privileges, capabilities dropped" {
    block="$(service_block "$OVERLAY" clamav)"
    [ -n "$block" ]
    grep -qE '^    mem_limit: [^[:space:]]+' <<< "$block"
    grep -qE '^      - no-new-privileges:true[[:space:]]*$' <<< "$block"
    grep -qE '^    cap_drop:[[:space:]]*$' <<< "$block"
    grep -qE '^      - ALL[[:space:]]*$' <<< "$block"
    # Only what the entrypoint needs (chown of the volume, privilege drop).
    added="$(awk '/^    cap_add:/ { inside = 1; next } inside && /^      - / { print $2; next } inside { exit }' <<< "$block" | sort | tr '\n' ' ')"
    [ "$added" = "CHOWN DAC_OVERRIDE FOWNER SETGID SETUID " ]
}

@test "overlay and dev profile: the same digest-pinned image and the same clamd.conf, read-only" {
    overlay_image="$(service_block "$OVERLAY" clamav | awk '$1 == "image:" { print $2 }')"
    dev_image="$(service_block "$DEV_COMPOSE" clamav | awk '$1 == "image:" { print $2 }')"
    [[ "$overlay_image" =~ ^clamav/clamav:[0-9][^@]*@sha256:[0-9a-f]{64}$ ]]
    [ "$overlay_image" = "$dev_image" ]
    service_block "$OVERLAY" clamav | grep -qE '^      - \./clamav/clamd\.conf:/etc/clamav/clamd\.conf:ro$'
    service_block "$DEV_COMPOSE" clamav \
        | grep -qE '^      - \./terraform/single-server/clamav/clamd\.conf:/etc/clamav/clamd\.conf:ro$'
    # The dev service is opt-in.
    service_block "$DEV_COMPOSE" clamav | grep -qE '^    profiles: \["clamav"\]$'
}

@test "the API's CLAMD_STREAM_MAX_BYTES default is still the value clamd.conf is checked against" {
    run grep -A2 -E '^    clamd_stream_max_bytes: int = Field\($' "$SETTINGS"
    [ "$status" -eq 0 ]
    [[ "$output" == *"default=100 * 1024 * 1024,"* ]]
    [ "$STREAM_MAX_DEFAULT" -eq $(( 100 * 1024 * 1024 )) ]
}
