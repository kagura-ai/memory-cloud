#!/bin/bash
# =============================================================================
# Kagura Memory Cloud — GCE startup script
# =============================================================================
# This script runs once on VM boot (via metadata_startup_script).
# Its only job is to prepare the VM so an operator can SSH in and run
# `docker compose up -d` to bring the stack online.
#
# It deliberately does NOT clone the application repo or start containers —
# the operator does that manually after reviewing secrets and Origin CA cert.
# =============================================================================

set -euo pipefail

LOG=/var/log/kagura-startup.log
exec > >(tee -a "$LOG") 2>&1

echo "=== kagura startup: $(date -u) ==="

# Avoid apt interactive prompts.
export DEBIAN_FRONTEND=noninteractive

# -----------------------------------------------------------------------------
# Base packages
# -----------------------------------------------------------------------------
apt-get update -y
apt-get install -y \
  ca-certificates \
  curl \
  gnupg \
  lsb-release \
  git \
  jq \
  ufw \
  unattended-upgrades

# -----------------------------------------------------------------------------
# Docker CE + compose plugin (official Docker apt repo)
# -----------------------------------------------------------------------------
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update -y
apt-get install -y \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin

# Pin docker packages so unattended-upgrades can't break the stack on a
# background apt run.
apt-mark hold docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# -----------------------------------------------------------------------------
# Docker daemon hardening
#   - json-file log driver with rotation so /var/lib/docker/containers
#     doesn't fill the disk
#   - overlay2 storage driver (default on Ubuntu 22.04, set explicitly so
#     nothing ever falls back to vfs)
# -----------------------------------------------------------------------------
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "3"
  },
  "storage-driver": "overlay2"
}
JSON

systemctl enable --now docker
systemctl restart docker

# Make sure the docker group exists so any OS Login user can be added to it
# with a single `sudo usermod -aG docker $USER && newgrp docker` after SSH.
groupadd -f docker

# -----------------------------------------------------------------------------
# Kagura working directory
# -----------------------------------------------------------------------------
install -d -m 0755 /opt/kagura-memory
install -d -m 0700 /var/lib/kagura
install -d -m 0700 /var/lib/kagura/origin-ca
install -d -m 0755 /var/lib/kagura/volumes

# Caddy extension point for sibling services co-resident on this VM (e.g.
# kagura-memory-ai-worker). They drop *.caddy vhost files here; the caddy
# container bind-mounts this read-only and Caddyfile imports it. root-owned
# 0755: world-readable so the container can read it, root-write so only an
# operator with sudo can add vhost files. See README "Caddy extension point".
install -d -m 0755 -o root -g root /opt/kagura-caddy-extra

# -----------------------------------------------------------------------------
# Systemd unit — bring the compose stack back up on reboot
#   The operator enables this with `sudo systemctl enable kagura-memory`
#   AFTER the first manual `docker compose up` and verification. We do not
#   enable it eagerly so the first boot is hand-driven.
# -----------------------------------------------------------------------------
cat > /etc/systemd/system/kagura-memory.service <<'UNIT'
[Unit]
Description=Kagura Memory Cloud docker-compose
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/kagura-memory/src/terraform/single-server
ExecStart=/usr/bin/docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
ExecStop=/usr/bin/docker compose -f docker-compose.prod.yml --env-file .env.prod down
TimeoutStartSec=600

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload

# -----------------------------------------------------------------------------
# Weekly docker prune — keeps /var/lib/docker from filling up with dangling
# images and build cache after repeated `docker compose build` runs.
# -----------------------------------------------------------------------------
cat > /etc/cron.weekly/docker-prune <<'CRON'
#!/bin/sh
set -eu
{
  echo "=== docker prune $(date -u) ==="
  /usr/bin/docker system prune -af --filter "until=168h"
  /usr/bin/docker volume prune -f --filter "label!=keep"
} >> /var/log/docker-prune.log 2>&1
CRON
chmod +x /etc/cron.weekly/docker-prune

# -----------------------------------------------------------------------------
# Automatic security updates (low-effort OS patching)
# -----------------------------------------------------------------------------
dpkg-reconfigure -f noninteractive unattended-upgrades || true

# -----------------------------------------------------------------------------
# Host-level firewall (defence in depth; GCP firewall is primary)
# -----------------------------------------------------------------------------
# We do NOT enable ufw by default here because a misconfigured ufw can lock
# the operator out. The GCP firewall (main.tf) is the authoritative control.
# Operators who want host-level ufw can enable it manually after verifying
# SSH/IAP still works.

# -----------------------------------------------------------------------------
# Completion marker — tail the serial console until you see this line:
#   gcloud compute instances get-serial-port-output <vm-name>
# -----------------------------------------------------------------------------
echo "=== kagura startup complete: $(date -u) ==="
echo "=== kagura startup complete: ready for docker compose up ==="

cat <<'HINT'
Next steps for the operator:
  1. gcloud compute ssh <vm-name> --tunnel-through-iap
  2. sudo usermod -aG docker $USER && newgrp docker
  3. sudo mkdir -p /opt/kagura-memory && sudo chown $USER /opt/kagura-memory
  4. cd /opt/kagura-memory
  5. git clone https://github.com/kagura-ai/memory-cloud.git src
  6. cd src/terraform/single-server
  7. scp Cloudflare origin cert/key to /var/lib/kagura/origin-ca/
  8. cp .env.prod.example .env.prod && vim .env.prod
  9. docker compose -f docker-compose.prod.yml up -d --build
 10. docker compose exec api alembic upgrade head
 11. docker compose exec api python -m src.cli.create_admin
 12. sudo systemctl enable kagura-memory   # auto-restart on reboot
HINT
