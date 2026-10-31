terraform {
  required_version = "~> 1.17"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.9"
    }
  }

  # Bucket створено вручну (infra/bootstrap/README.md, FR-IAC-008).
  backend "gcs" {
    bucket = "apps-deploy-512911-tfstate"
    prefix = "terraform/state"
  }
}
