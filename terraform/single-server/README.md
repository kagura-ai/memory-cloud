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

# On the VM:
sudo mkdir -p /opt/kagura-memory
sudo chown -R $USER /opt/kagura-memory
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

Start everything:

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

The first build takes several minutes (backend + frontend images).

## Step 6 — Initialize the database and create the first admin

```bash
docker compose -f docker-compose.prod.yml exec api alembic upgrade head

docker compose -f docker-compose.prod.yml exec api \
  python -m src.cli.create_admin
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

### Update to a new release

```bash
# On the VM
cd /opt/kagura-memory/src
git pull
cd terraform/single-server
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml exec api alembic upgrade head
```

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
