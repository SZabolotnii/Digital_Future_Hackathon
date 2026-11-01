variable "project_id" {
  type = string
}

variable "github_repository" {
  type        = string
  description = "owner/name репозиторію на GitHub."
}

variable "github_repository_id" {
  type        = string
  description = "Числовий ID репозиторію: не змінюється при перейменуванні чи передачі."
}

variable "gha_service_account_id" {
  type = string
}

data "google_project" "this" {
  project_id = var.project_id
}

# Сервісний агент Cloud Build тримає токен GitHub-підключення в Secret Manager.
# Саме підключення ставиться вручну (infra/bootstrap/README.md, крок 4).
resource "google_project_iam_member" "cloudbuild_agent_secrets" {
  project = var.project_id
  role    = "roles/secretmanager.admin"
  member  = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-cloudbuild.iam.gserviceaccount.com"
}

# FR-CICD-004: GitHub OIDC без JSON-ключів. Пускає лише цей репозиторій і лише main.
resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-oidc"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  attribute_condition = "assertion.repository_id == '${var.github_repository_id}' && assertion.ref == 'refs/heads/main'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "gha_wif" {
  service_account_id = var.gha_service_account_id
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}

output "wif_provider" {
  value = google_iam_workload_identity_pool_provider.github.name
}

variable "region" {
  type = string
}

variable "build_service_account_id" {
  type = string
}

# Підключення lmc-github створено вручну (OAuth і GitHub App ставляться через
# браузер, infra/bootstrap/README.md); тут лише репозиторій у ньому і trigger.
resource "google_cloudbuildv2_repository" "app" {
  project           = var.project_id
  location          = var.region
  name              = split("/", var.github_repository)[1]
  parent_connection = "projects/${var.project_id}/locations/${var.region}/connections/lmc-github"
  remote_uri        = "https://github.com/${var.github_repository}.git"
}

# ADR-06: без події push, лише ручний запуск з конкретним SHA з GitHub Actions.
resource "google_cloudbuild_trigger" "deploy" {
  project         = var.project_id
  location        = var.region
  name            = "lmc-deploy"
  description     = "Build, deploy and smoke livemedcard-demo; run by GitHub Actions after green tests"
  service_account = var.build_service_account_id

  source_to_build {
    repository = google_cloudbuildv2_repository.app.id
    ref        = "refs/heads/main"
    repo_type  = "GITHUB"
  }

  # Без revision: cloudbuild.yaml береться з того самого SHA, що пройшов тести
  # (--sha у `gcloud builds triggers run`), а не з поточної вершини main.
  git_file_source {
    path       = "cloudbuild.yaml"
    repository = google_cloudbuildv2_repository.app.id
    repo_type  = "GITHUB"
  }
}
