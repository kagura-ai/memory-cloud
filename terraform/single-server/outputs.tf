output "vm_name" {
  description = "Name of the Compute Engine VM."
  value       = google_compute_instance.vm.name
}

output "vm_zone" {
  description = "Zone the VM lives in."
  value       = google_compute_instance.vm.zone
}

output "vm_external_ip" {
  description = "Static external IPv4 address. Set a Cloudflare A record for your domain pointing here (Proxied / orange cloud ON)."
  value       = google_compute_address.vm.address
}

output "service_account_email" {
  description = "Service account attached to the VM."
  value       = google_service_account.vm.email
}

output "assets_bucket" {
  description = "GCS bucket reserved for future multimodal uploads. Backend integration is tracked in a separate issue."
  value       = google_storage_bucket.assets.name
}

output "ssh_iap_command" {
  description = "Recommended SSH command using IAP tunnel (works even when admin_ssh_cidrs is empty)."
  value       = "gcloud compute ssh ${google_compute_instance.vm.name} --zone ${google_compute_instance.vm.zone} --project ${var.project_id} --tunnel-through-iap"
}

output "dns_setup_hint" {
  description = "Reminder of the Cloudflare DNS record to create for the domain."
  value       = <<-EOT
    Cloudflare DNS setup for ${var.domain}:

      Type:    A
      Name:    ${var.domain}
      Content: ${google_compute_address.vm.address}
      Proxy:   Proxied (orange cloud)
      TTL:     Auto

    Then in SSL/TLS:
      - Mode: Full (strict)
      - Origin Server → Create Certificate (RSA 2048, 15 years)
      - scp the generated cert + key to /var/lib/kagura/origin-ca/ on the VM
  EOT
}
