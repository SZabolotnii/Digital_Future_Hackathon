variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

# Репозиторій, який створив `gcloud run deploy --source`. Після переходу деплою
# на Cloud Build його замінить репозиторій `livemedcard` (FR-IAC-005).
resource "google_artifact_registry_repository" "source_deploy" {
  project       = var.project_id
  location      = var.region
  repository_id = "cloud-run-source-deploy"
  description   = "Cloud Run Source Deployments"
  format        = "DOCKER"
}
