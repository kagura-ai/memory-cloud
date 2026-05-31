# Kagura Memory Cloud — Single-Server Self-Host

Terraform + docker-compose configuration to run the full Kagura Memory Cloud
stack (API + Web UI + PostgreSQL + Qdrant + Redis + Caddy) on a single
Google Compute Engine VM behind Cloudflare.

## What you get

- **1 Compute Engine VM** (e2-medium by default, ~$40/mo in Tokyo) running
  Ubuntu 22.04 LTS with Docker pre-installed
- **Static external IPv4** attached to the VM
- **Firewall rules**: 443 from Cloudflare edge only, SSH via IAP tunnel (no
  public port 22 by default)
- **Minimum-privilege Service Account** attached to the VM with GCS write
  access to one bucket only
- **GCS bucket** reserved for future multimodal uploads (backend integration
  is tracked in a follow-up issue — the bucket is unused until then)
- **Caddy reverse proxy** terminating TLS with a Cloudflare Origin CA cert
- **docker-compose** stack with optional Ollama local-embeddings override

## Not included (deliberately)

- No automatic TLS via Let's Encrypt. Cloudflare Origin CA cert is the
  authoritative TLS material (15 year validity, no renewal hassle).
- No managed databases (Cloud SQL / Memorystore). All state lives on the VM.
- No backend GCS integration yet — the bucket exists but nothing writes to
  it until the multimodal issue lands.
- No automated snapshots / backups. Use `gcloud compute disks snapshot`
  manually until the backup scheduling issue lands.
- No Cloudflare provider in Terraform. DNS + Origin CA are configured
  manually in the Cloudflare dashboard — it's a 5-minute one-time setup.

## OS choice — Ubuntu 22.04 LTS

The VM image is pinned to `ubuntu-os-cloud/ubuntu-2204-lts`.

- **Ubuntu 22.04 LTS** — supported until April 2027, has an officially
  maintained Docker CE apt repository, and works with every tool you're
  likely to reach for (cloudflared, snap, standard apt packages). `startup.sh`
  installs Docker from the upstream repository — **Docker is NOT preinstalled**
  on Ubuntu images; expect `startup.sh` to take 2-3 minutes on first boot.
- **Container-Optimized OS (COS)** — not used. COS ships with Docker
  preinstalled but has a read-only root filesystem and heavy restrictions on
  where you can write files. Running docker-compose with host volume mounts
  on COS is painful. COS is designed for GKE nodes, not multi-service
  self-host stacks.
- **Debian 12** — viable, but you'd need to set up the Docker apt repo
  anyway and the LTS window is shorter than Ubuntu's. Ubuntu wins on
  ecosystem familiarity.

## How long startup takes

`terraform apply` → first reachable `docker compose up -d`:

1. VM create (~30 seconds)
2. Boot + `startup.sh` (~2-3 minutes: apt update, Docker CE install, daemon
   config, systemd unit, cron, prune rules)
3. First `docker compose up -d --build` (5-10 minutes on e2-medium for the
   backend and frontend images)

You can watch the startup script's progress via the serial console:

```bash
gcloud compute instances get-serial-port-output "$(terraform output -raw vm_name)" \
  --zone "$(terraform output -raw vm_zone)" --project "$PROJECT_ID" \
  | grep -a kagura
```

Look for `=== kagura startup complete: ready for docker compose up ===`.

## Prerequisites

1. **GCP project** with billing enabled
2. **`gcloud` CLI** installed and authenticated (`gcloud auth login`,
   `gcloud auth application-default login`)
3. **Terraform** ≥ 1.6
4. **A domain** you control (e.g., `memory.example.com`) with its DNS
   delegated to Cloudflare
5. **Google OAuth client** created in the Cloud Console (for admin sign-in)

## Step 1 — Prepare the GCP project

Replace `YOUR_PROJECT_ID` and `YOUR_TFSTATE_BUCKET` with your own values.

```bash
export PROJECT_ID=YOUR_PROJECT_ID
export TFSTATE_BUCKET=YOUR_TFSTATE_BUCKET   # must be globally unique
export REGION=asia-northeast1

gcloud config set project "$PROJECT_ID"

# Enable billing if you haven't already:
# https://console.cloud.google.com/billing/linkedaccount?project=$PROJECT_ID

# Create the Terraform state bucket (one-time)
gcloud storage buckets create "gs://${TFSTATE_BUCKET}" \
  --location="${REGION}" \
  --uniform-bucket-level-access

gcloud storage buckets update "gs://${TFSTATE_BUCKET}" \
  --versioning
```

## Step 2 — Configure Terraform

```bash
cd terraform/single-server

cp terraform.tfvars.example terraform.tfvars
vim terraform.tfvars      # set project_id and domain, at minimum
```

Initialize with the backend pointing at your state bucket:

```bash
terraform init \
  -backend-config="bucket=${TFSTATE_BUCKET}" \
  -backend-config="prefix=kagura-memory/single-server"
```

## Step 3 — Apply

```bash
terraform plan    # review carefully
terraform apply
```

On success you'll see outputs including `vm_external_ip` and a
`dns_setup_hint` that tells you exactly which Cloudflare record to create.

## Step 4 — Cloudflare DNS + Origin CA certificate

In the Cloudflare dashboard for your zone:

1. **DNS → Records → Add**
   - Type: `A`
   - Name: your subdomain (e.g. `memory`)
   - IPv4: the `vm_external_ip` from Terraform output
   - Proxy status: **Proxied** (orange cloud ON)
   - TTL: Auto

2. **SSL/TLS → Overview**
   - Mode: **Full (strict)**

3. **SSL/TLS → Origin Server → Create Certificate**
   - Key type: RSA (2048)
   - Hostnames: your domain (e.g. `memory.example.com`) plus `*.example.com`
     if you might need subdomains
   - Certificate Validity: 15 years
   - **Save the certificate and private key locally** — Cloudflare only
     shows them once

## Step 5 — Bring up the stack on the VM

```bash
# SSH into the VM using IAP tunnel (no public SSH needed)
gcloud compute ssh "$(terraform output -raw vm_name)" \
  --zone "$(terraform output -raw vm_zone)" \
  --project "$PROJECT_ID" \
  --tunnel-through-iap

# On the VM — add yourself to the docker group so you can skip sudo
sudo usermod -aG docker "$USER"
newgrp docker

# Prepare the working directory and pull the source
sudo mkdir -p /opt/kagura-memory
sudo chown -R "$USER" /opt/kagura-memory
cd /opt/kagura-memory
git clone https://github.com/kagura-ai/memory-cloud.git src
cd src/terraform/single-server
```

Copy the Origin CA cert + key from your workstation to the VM (run from
the workstation, not the VM):

```bash
# From your workstation
gcloud compute scp \
  ./cloudflare-origin-cert.pem \
  ./cloudflare-origin-key.pem \
  "$(terraform output -raw vm_name):/tmp/" \
  --zone "$(terraform output -raw vm_zone)" \
  --tunnel-through-iap
```

Back on the VM, move them into the Caddy mount:

```bash
sudo mv /tmp/cloudflare-origin-cert.pem /var/lib/kagura/origin-ca/cert.pem
sudo mv /tmp/cloudflare-origin-key.pem  /var/lib/kagura/origin-ca/key.pem
sudo chmod 0640 /var/lib/kagura/origin-ca/*
```

Edit the Caddyfile to point at your actual domain (it defaults to
`memory.kagura-ai.com`):

```bash
cd /opt/kagura-memory/src/terraform/single-server
sed -i 's/memory.kagura-ai.com/YOUR_DOMAIN/' Caddyfile
```

Create `.env.prod`:

```bash
cp .env.prod.example .env.prod
vim .env.prod      # set KAGURA_DOMAIN, DB_PASSWORD, QDRANT_API_KEY,
                   # API_KEY_SECRET, JWT_SECRET, Google OAuth client, etc.
```

Start everything (initial setup starts both API colors; Caddy defaults to
`api-blue`):

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
echo "blue" > /opt/kagura-memory/active-color
```

The first build takes several minutes (backend + frontend images).

Once everything is healthy, enable the systemd unit so the stack comes back
after a reboot:

```bash
sudo systemctl enable kagura-memory
```

## Step 6 — Initialize the database and create the first admin

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod \
  exec api-blue alembic upgrade head

docker compose -f docker-compose.prod.yml --env-file .env.prod \
  exec api-blue python -m src.cli.create_admin
```

## Step 7 — Verify

```bash
# From anywhere
curl -sSf https://YOUR_DOMAIN/health
```

Expected: a JSON response with `status: "healthy"` (or similar).

Open `https://YOUR_DOMAIN` in a browser, sign in with the admin account
you just created, or via Google OAuth once the Google client is wired up.

## Optional: Ollama local embeddings

The default stack uses OpenAI embeddings. To run with Ollama instead,
bump the VM size first (e2-medium is not enough):

```hcl
# terraform.tfvars
machine_type      = "n2-highmem-4"   # 4 vCPU / 32 GB
boot_disk_size_gb = 100
```

Re-apply Terraform, then on the VM:

```bash
docker compose \
  -f docker-compose.prod.yml \
  -f docker-compose.ollama.yml \
  up -d

# Pre-pull an embedding model
docker compose exec ollama ollama pull nomic-embed-text
```

## Operations

### SSH via IAP

No direct port 22 exposure needed:

```bash
gcloud compute ssh kagura-memory-vm \
  --zone asia-northeast1-a \
  --project $PROJECT_ID \
  --tunnel-through-iap
```

### Logs

```bash
docker compose -f docker-compose.prod.yml logs -f caddy
docker compose -f docker-compose.prod.yml logs -f api
```

### Manual snapshot

```bash
gcloud compute disks snapshot kagura-memory-vm \
  --zone asia-northeast1-a \
  --snapshot-names "kagura-memory-$(date +%Y%m%d-%H%M)"
```

### Update to a new release (zero-downtime)

The stack uses **blue-green deploy** for the API container. Caddy always
routes to one color; the deploy script builds the other, waits for it to
be ready, switches Caddy, then drains and stops the old color.

```bash
# On the VM
cd /opt/kagura-memory/src
git fetch && git reset --hard origin/main

cd terraform/single-server
./scripts/deploy.sh           # zero-downtime blue-green deploy
```

The script handles building, migrations, readiness checks, Caddy reload,
and draining automatically. Use `./scripts/deploy.sh --status` to see
which color is active, or `./scripts/deploy.sh --rollback` to switch back.

Tunable environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `READINESS_TIMEOUT` | 60 | Seconds to wait for `/readiness` |
| `DRAIN_TIMEOUT` | 30 | Seconds to drain old container |

### Update the frontend (in-place rebuild)

The API uses blue-green; the frontend (`kagura-web`) is rebuilt **in
place** because its `NEXT_PUBLIC_*` build args (e.g. `NEXT_PUBLIC_API_URL`
derived from `${KAGURA_DOMAIN}`) are baked into the Next.js bundle at
build time. Blue-green doesn't apply.

The full `--web` run takes roughly **3–5 minutes** on the default e2-medium
VM — almost all of it is `npm ci` + `next build` inside the new image. The
container restart itself, and the readiness check that follows it, complete
in under a minute. The frontend is unavailable during the restart window
(the API stays up).

```bash
# On the VM
cd /opt/kagura-memory/src
git fetch && git reset --hard origin/main

cd terraform/single-server
./scripts/deploy.sh --web    # rebuild + restart kagura-web, in-place
```

`--web` uses the same `dc()` wrapper as the API deploy, so
`--env-file .env.prod` is always present and `${KAGURA_DOMAIN}` interpolates
correctly into the `NEXT_PUBLIC_*_URL` build args. Forgetting `--env-file`
when rebuilding `web` manually produced a silently broken bundle
(`TypeError: Invalid URL` at build time — see #643 root cause); `--web`
removes that footgun by funneling the call through `dc()`.

Under the hood, `--web` runs three steps:

1. `dc build --no-cache web` — fresh build, picks up source + build args
2. `dc up -d --no-deps --force-recreate web` — restart in place;
   `--no-deps` keeps `postgres / redis / qdrant / caddy / api-*` untouched
3. Wait for `/api/health` from inside the container (same path as the
   Dockerfile `HEALTHCHECK`)

Tunable environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `WEB_READINESS_TIMEOUT` | 30 | Seconds to wait for web `/api/health` **after** `dc up` returns (does not bound the build itself) |
| `WEB_READINESS_INTERVAL` | 2 | Seconds between health-check attempts |

**Web rollback**: there is no `--web --rollback`. The frontend is
rebuilt in place, so the previous image is no longer addressable. To
roll back the frontend, `git revert` the offending commit and re-run
`./scripts/deploy.sh --web`.

**Regression check after `--web`** — the existing blue-green flow must
remain untouched:

1. `./scripts/deploy.sh --status` — note the current active API color
2. `./scripts/deploy.sh --web` — rebuild the frontend
3. `./scripts/deploy.sh --status` — confirm the active API color has
   **not** moved (this proves `--web` did not touch the API containers)
4. (optional) `./scripts/deploy.sh --rollback` followed by
   `./scripts/deploy.sh` — confirm API blue-green still cycles cleanly

### Migration discipline

Database migrations run **before** the Caddy switch so both old and new
containers can coexist on the same schema:

- **Forward-compatible** (additive columns, new tables): always safe.
  `deploy.sh` starts the new container, waits for readiness, then runs
  `alembic upgrade head` on the new container before switching Caddy.
- **Backward-incompatible** (drop columns, type changes): require a
  **two-phase deploy**:
  1. First deploy: add new columns/tables, update code to use both old and new
  2. Second deploy: remove old columns after the previous deploy is verified

### Rollback

If the new color misbehaves after switching:

```bash
./scripts/deploy.sh --rollback
```

This starts the previous color if it's not running, waits for readiness,
then flips Caddy back and reloads. Safe to run at any time after a deploy.

### Caddy extension point (sibling services)

Other services co-resident on this VM (for example
`kagura-memory-ai-worker`'s webhook receiver at `aw.kagura-ai.com`) can publish
their own HTTPS vhost through this server's Caddy **without any further change
to the memory-cloud repository**. The mechanism is a one-time extension point:

- `Caddyfile.tpl` ends with a top-level `import /opt/kagura-caddy-extra/*.caddy`.
- The caddy container bind-mounts `/opt/kagura-caddy-extra` read-only
  (`docker-compose.prod.yml`).
- `startup.sh` provisions `/opt/kagura-caddy-extra` (root-owned, `0755`).

A sibling service publishes a vhost by dropping a `*.caddy` file into
`/opt/kagura-caddy-extra/` on the host (requires sudo — the directory is
root-owned). Each file may contain full top-level vhost blocks, e.g.:

```caddy
# /opt/kagura-caddy-extra/aw.caddy   (owned by the ai-worker operator)
aw.kagura-ai.com {
	tls /etc/caddy/origin-ca/cert.pem /etc/caddy/origin-ca/key.pem
	# Upstream MUST be reachable from inside the kagura-caddy container — see
	# "Reaching the sibling's upstream" below. The host-gateway form works
	# regardless of which compose project the sibling runs in:
	reverse_proxy host.docker.internal:9000
}
```

**Reaching the sibling's upstream.** `kagura-caddy` runs on this compose
project's default network (there is no explicit `networks:` block in
`docker-compose.prod.yml`), so it resolves a sibling **service name** only if
the sibling is on that same network. A service from a *separate* compose project
is on a *different* network by default, so a bare `reverse_proxy ai-worker:9000`
will **not** resolve. Two working options:

- **Host-gateway (simplest, cross-project):** the sibling publishes a host port
  (e.g. `127.0.0.1:9000`) and the vhost proxies to `host.docker.internal:9000`
  (as in the example above). This requires Caddy to know the host gateway — add
  it once to the caddy service in `docker-compose.prod.yml`:

  ```yaml
  caddy:
    extra_hosts:
      - "host.docker.internal:host-gateway"
  ```

- **Shared network (service-name DNS):** the sibling's compose joins this
  project's network as an `external` network, after which `reverse_proxy
  ai-worker:9000` resolves by service name. Find the network name with
  `docker inspect kagura-caddy -f '{{json .NetworkSettings.Networks}}'` (it is
  this project's `*_default`), then in the sibling's compose:

  ```yaml
  networks:
    kagura_shared:
      external: true
      name: <kagura-caddy's network, e.g. single-server_default>
  services:
    ai-worker:
      networks: [kagura_shared]
  ```

**Applying changes — recreate vs reload (read this):**

| Situation | Command | Why |
|---|---|---|
| **First rollout of this mount** (deploying the change that adds the `/opt/kagura-caddy-extra` volume) | `docker compose -f docker-compose.prod.yml up -d caddy` | A bind mount is fixed at container **creation** time. `docker compose restart` (and `deploy.sh`'s `restart caddy`) restart the *existing* container and do **not** apply a newly-added mount — the directory would be invisible inside the container. The container must be **recreated** once. |
| **Adding / editing a `*.caddy` file** after the mount already exists | `docker compose -f docker-compose.prod.yml exec caddy caddy reload --config /etc/caddy/Caddyfile` | The mount is already present, so Caddy only needs to re-read config. A reload is sufficient and zero-downtime. |

> The issue that introduced this called for "confirm with `caddy reload`" — that
> is correct for steady-state `*.caddy` edits, but **not** for the first rollout
> of the mount itself, which requires the `up -d caddy` recreate above.

**Verifying a rollout** (the recreate momentarily drops `:80`/`:443` as the
container is replaced — do it in a maintenance window or accept a brief blip):

```bash
cd /opt/kagura-memory/src/terraform/single-server

# 1. Validate BEFORE bouncing — never recreate the container on a broken config.
docker compose -f docker-compose.prod.yml exec caddy \
  caddy validate --config /etc/caddy/Caddyfile

# 2. Apply: recreate (first rollout of the mount) or reload (steady-state edit).
docker compose -f docker-compose.prod.yml up -d caddy        # first rollout
# docker compose -f docker-compose.prod.yml exec caddy \
#   caddy reload --config /etc/caddy/Caddyfile                # later *.caddy edits

# 3. Confirm EXISTING vhosts are unaffected (acceptance: "no impact on existing
#    vhosts") — re-check the main endpoints AFTER the recreate, not just the new one.
curl -fsS https://memory.kagura-ai.com/health        # expect 200
curl -fsS -o /dev/null -w '%{http_code}\n' https://memory.kagura-ai.com/   # main site

# 4. Confirm the NEW sibling vhost responds.
curl -fsS https://<sibling-domain>/...               # e.g. aw.kagura-ai.com
```

An empty `/opt/kagura-caddy-extra/` is valid — Caddy tolerates the glob import
matching zero files, so the extension point is harmless until a sibling uses it.

## Teardown

```bash
terraform destroy
```

By default the assets bucket is **not** force-deleted; empty it manually if
you want `destroy` to remove it, or set
`assets_bucket_force_destroy = true` in `terraform.tfvars`.

## Cost (rough, Tokyo region)

| Item | Monthly |
|---|---|
| e2-medium VM | ~$34 |
| Static external IP | ~$3 |
| pd-balanced 30GB boot disk | ~$4 |
| GCS assets bucket (empty → few GB) | ~$0-2 |
| Egress via Cloudflare | ~$2-5 |
| **Total** | **~$45-50/mo** |

## Security notes

- The VM's service account has write access **only** to the assets bucket.
  It has no other GCP permissions.
- Port 22 is closed at the GCP firewall by default. Use IAP for SSH.
- Port 443 is allowed only from Cloudflare's published IPv4 ranges. Refresh
  `cloudflare_ipv4_cidrs` in `variables.tf` periodically from
  <https://www.cloudflare.com/ips-v4>.
- Cloudflare Origin CA cert + key live at `/var/lib/kagura/origin-ca/` with
  mode `0640` and are mounted read-only into the Caddy container.
- Secrets in `.env.prod` are VM-local. Moving them to GCP Secret Manager is
  a follow-up issue.
