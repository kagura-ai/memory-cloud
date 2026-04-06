terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.40"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Remote state backend.
  # Create the bucket manually once, then run:
  #   terraform init \
  #     -backend-config="bucket=YOUR_TFSTATE_BUCKET" \
  #     -backend-config="prefix=kagura-memory/single-server"
  backend "gcs" {
    # bucket and prefix are provided via -backend-config at init time
    # so this template stays project-agnostic.
  }
}
