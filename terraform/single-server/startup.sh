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

systemctl enable --now docker

# -----------------------------------------------------------------------------
# Kagura working directory
# -----------------------------------------------------------------------------
install -d -m 0755 /opt/kagura-memory
install -d -m 0700 /var/lib/kagura
install -d -m 0700 /var/lib/kagura/origin-ca
install -d -m 0755 /var/lib/kagura/volumes

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

echo "=== kagura startup complete: $(date -u) ==="
echo "Next steps for the operator:"
echo "  1. gcloud compute ssh <vm-name> --tunnel-through-iap"
echo "  2. cd /opt/kagura-memory"
echo "  3. scp docker-compose.prod.yml + Caddyfile + .env.prod + origin-ca/ from workstation"
echo "  4. docker compose -f docker-compose.prod.yml up -d"
echo "  5. docker compose exec api alembic upgrade head"
echo "  6. docker compose exec api python -m src.cli.create_admin"
