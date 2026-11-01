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

# FR-IAC-005: образи за git SHA, зберігаються 10 останніх. Незмінні теги
# вимкнено: з ними cleanup policy не видаляє тегованих образів, а тег є в
# кожного. Перезапис тегу натомість відсікає cloudbuild.yaml (крок image-exists).
resource "google_artifact_registry_repository" "livemedcard" {
  project       = var.project_id
  location      = var.region
  repository_id = "livemedcard"
  description   = "LiveMedCard images, tagged by git SHA"
  format        = "DOCKER"

  docker_config {
    immutable_tags = false
  }

  cleanup_policy_dry_run = false

  # Політика keep переважає delete: видаляється все, крім 10 найновіших.
  cleanup_policies {
    id     = "keep-10-most-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }

  cleanup_policies {
    id     = "delete-the-rest"
    action = "DELETE"
    condition {
      tag_state = "ANY"
    }
  }
}

output "image" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.livemedcard.repository_id}/livemedcard"
}
