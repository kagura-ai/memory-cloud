# =============================================================================
# Kagura Memory Cloud — single-server self-host
# =============================================================================
# One Compute Engine VM running the full docker-compose stack
# (Caddy + API + Web + PostgreSQL + Qdrant + Redis) behind Cloudflare
# (Proxied) with a GCS bucket reserved for future multimodal uploads.
#
# See README.md for the full walkthrough.
# =============================================================================

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

# -----------------------------------------------------------------------------
# APIs — enable the services this stack needs.
# -----------------------------------------------------------------------------

locals {
  required_services = [
    "compute.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
  ]
}

resource "google_project_service" "required" {
  for_each = toset(local.required_services)

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# -----------------------------------------------------------------------------
# Networking — use the default VPC for simplicity.
# -----------------------------------------------------------------------------

data "google_compute_network" "default" {
  name = "default"

  depends_on = [google_project_service.required]
}

resource "google_compute_address" "vm" {
  name         = "${var.name_prefix}-ip"
  region       = var.region
  address_type = "EXTERNAL"
  description  = "Static external IP for the Kagura Memory Cloud single-server VM. Point Cloudflare's A record at this address."

  depends_on = [google_project_service.required]
}

# -----------------------------------------------------------------------------
# Firewall
#   443 — from Cloudflare IP ranges only
#   22  — from admin_ssh_cidrs only (default empty → port fully closed,
#         use `gcloud compute ssh --tunnel-through-iap` instead)
# -----------------------------------------------------------------------------

resource "google_compute_firewall" "cloudflare_https" {
  name        = "${var.name_prefix}-allow-cloudflare-https"
  network     = data.google_compute_network.default.name
  description = "Allow HTTPS from Cloudflare edge IPs only. Refresh cloudflare_ipv4_cidrs from https://www.cloudflare.com/ips-v4 periodically."

  direction = "INGRESS"
  priority  = 1000

  source_ranges = var.cloudflare_ipv4_cidrs
  target_tags   = ["${var.name_prefix}-vm"]

  allow {
    protocol = "tcp"
    ports    = ["443"]
  }
}

resource "google_compute_firewall" "admin_ssh" {
  count = length(var.admin_ssh_cidrs) > 0 ? 1 : 0

  name        = "${var.name_prefix}-allow-admin-ssh"
  network     = data.google_compute_network.default.name
  description = "Allow SSH from admin CIDRs. Set admin_ssh_cidrs=[] to disable this rule and use IAP instead."

  direction = "INGRESS"
  priority  = 1000

  source_ranges = var.admin_ssh_cidrs
  target_tags   = ["${var.name_prefix}-vm"]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# IAP (Identity-Aware Proxy) TCP forwarding source range — always allowed so
# `gcloud compute ssh --tunnel-through-iap` keeps working even when
# admin_ssh_cidrs is empty.
resource "google_compute_firewall" "iap_ssh" {
  name        = "${var.name_prefix}-allow-iap-ssh"
  network     = data.google_compute_network.default.name
  description = "Allow SSH from Google IAP for `gcloud compute ssh --tunnel-through-iap`."

  direction = "INGRESS"
  priority  = 1000

  source_ranges = ["35.235.240.0/20"]
  target_tags   = ["${var.name_prefix}-vm"]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# -----------------------------------------------------------------------------
# Service Account
#   Minimum-privilege SA attached to the VM. Gets write access only to the
#   assets bucket; no project-wide roles.
# -----------------------------------------------------------------------------

resource "google_service_account" "vm" {
  account_id   = "${var.name_prefix}-vm"
  display_name = "Kagura Memory Cloud — single-server VM"
  description  = "Service account attached to the Kagura Memory Cloud VM. Scoped to GCS write access on the assets bucket only."

  depends_on = [google_project_service.required]
}

# -----------------------------------------------------------------------------
# GCS bucket — multimodal assets
#   Created now, backend integration comes in a follow-up issue.
# -----------------------------------------------------------------------------

resource "google_storage_bucket" "assets" {
  name     = "${var.name_prefix}-${var.project_id}-${var.assets_bucket_suffix}"
  location = var.region

  uniform_bucket_level_access = true
  force_destroy               = var.assets_bucket_force_destroy

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      num_newer_versions = 10
    }
    action {
      type = "Delete"
    }
  }

  labels = var.labels

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket_iam_member" "vm_assets_rw" {
  bucket = google_storage_bucket.assets.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.vm.email}"
}

# -----------------------------------------------------------------------------
# Compute Engine VM
# -----------------------------------------------------------------------------

resource "google_compute_instance" "vm" {
  name         = "${var.name_prefix}-vm"
  machine_type = var.machine_type
  zone         = var.zone

  tags = ["${var.name_prefix}-vm", "http-server", "https-server"]

  boot_disk {
    initialize_params {
      image = var.boot_image
      size  = var.boot_disk_size_gb
      type  = var.boot_disk_type
    }
  }

  network_interface {
    network = data.google_compute_network.default.name

    access_config {
      nat_ip = google_compute_address.vm.address
    }
  }

  service_account {
    email = google_service_account.vm.email
    scopes = [
      "https://www.googleapis.com/auth/devstorage.read_write",
      "https://www.googleapis.com/auth/logging.write",
      "https://www.googleapis.com/auth/monitoring.write",
    ]
  }

  metadata = {
    enable-oslogin = "TRUE"
  }

  metadata_startup_script = file("${path.module}/startup.sh")

  labels = var.labels

  # Allow `terraform apply` updates without forcing replacement when only
  # metadata or labels change.
  allow_stopping_for_update = true

  depends_on = [
    google_project_service.required,
    google_compute_firewall.cloudflare_https,
  ]
}
