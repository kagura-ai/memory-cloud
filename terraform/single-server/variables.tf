variable "project_id" {
  description = "GCP project ID to deploy into."
  type        = string
}

variable "region" {
  description = "GCP region for regional resources (GCS bucket, subnetwork)."
  type        = string
  default     = "asia-northeast1"
}

variable "zone" {
  description = "GCP zone for the Compute Engine VM."
  type        = string
  default     = "asia-northeast1-a"
}

variable "name_prefix" {
  description = "Prefix applied to resource names (VM, IP, SA, firewall, bucket)."
  type        = string
  default     = "kagura-memory"
}

variable "domain" {
  description = "Public domain served by Caddy (e.g. memory.kagura-ai.com). Used for Caddy upstream check documentation only — DNS is managed in Cloudflare outside Terraform."
  type        = string
}

variable "machine_type" {
  description = "GCE machine type. e2-medium (2 vCPU / 4GB) is the minimum recommended for the full docker-compose stack."
  type        = string
  default     = "e2-medium"
}

variable "boot_disk_size_gb" {
  description = "Boot disk size in GB. 30GB is enough for the stack + a few weeks of logs and Qdrant data at soft-launch scale."
  type        = number
  default     = 30
}

variable "boot_disk_type" {
  description = "Boot disk type. pd-balanced is the default recommendation for mixed read/write workloads at this scale."
  type        = string
  default     = "pd-balanced"
}

variable "boot_image" {
  description = "Boot image family. Ubuntu 22.04 LTS is what startup.sh targets."
  type        = string
  default     = "ubuntu-os-cloud/ubuntu-2204-lts"
}

variable "admin_ssh_cidrs" {
  description = "CIDR ranges allowed to SSH into the VM on port 22. Keep this tight — default is your own IP only. Set to [] (empty) to disable port 22 entirely and rely on `gcloud compute ssh` over IAP."
  type        = list(string)
  default     = []
}

variable "cloudflare_ipv4_cidrs" {
  description = "Cloudflare's published IPv4 ranges allowed to reach the VM on port 443. Refresh from https://www.cloudflare.com/ips-v4 periodically."
  type        = list(string)
  default = [
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
  ]
}

variable "assets_bucket_suffix" {
  description = "Suffix appended to the multimodal assets bucket name (bucket name = {name_prefix}-{project_id}-{suffix}). The bucket is created for future multimodal uploads — backend integration is a separate issue."
  type        = string
  default     = "assets"
}

variable "assets_bucket_force_destroy" {
  description = "If true, `terraform destroy` will delete the assets bucket even if it contains objects. Keep false in real production."
  type        = bool
  default     = false
}

variable "labels" {
  description = "Labels applied to all resources that support them."
  type        = map(string)
  default = {
    app       = "kagura-memory"
    component = "single-server"
    managed   = "terraform"
  }
}
