variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

# Spec §3.4: жодного Owner/Editor на service accounts, ключів немає.
resource "google_service_account" "run_demo" {
  project      = var.project_id
  account_id   = "lmc-run-demo"
  display_name = "LiveMedCard demo runtime (Cloud Run)"
}

resource "google_service_account" "build" {
  project      = var.project_id
  account_id   = "lmc-build"
  display_name = "LiveMedCard build (Cloud Build)"
}

# GitHub Actions входить сюди через WIF (модуль cicd) і лише запускає Cloud Build.
resource "google_service_account" "gha" {
  project      = var.project_id
  account_id   = "lmc-gha"
  display_name = "LiveMedCard GitHub Actions (WIF)"
}

locals {
  project_roles = {
    "run_demo-logWriter"    = { sa = google_service_account.run_demo.member, role = "roles/logging.logWriter" }
    "run_demo-metricWriter" = { sa = google_service_account.run_demo.member, role = "roles/monitoring.metricWriter" }
    "build-runDeveloper"    = { sa = google_service_account.build.member, role = "roles/run.developer" }
    "build-arWriter"        = { sa = google_service_account.build.member, role = "roles/artifactregistry.writer" }
    "build-logWriter"       = { sa = google_service_account.build.member, role = "roles/logging.logWriter" }
    "gha-buildsEditor"      = { sa = google_service_account.gha.member, role = "roles/cloudbuild.builds.editor" }
  }
}

resource "google_project_iam_member" "this" {
  for_each = local.project_roles

  project = var.project_id
  role    = each.value.role
  member  = each.value.sa
}

# lmc-build розгортає сервіс від імені lmc-run-demo, і лише його.
resource "google_service_account_iam_member" "build_acts_as_run_demo" {
  service_account_id = google_service_account.run_demo.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.build.member
}

# Trigger збирає від імені lmc-build, тож той, хто його запускає, має право
# діяти як lmc-build. У Spec §3.4 цього рядка немає (див. додаток А).
resource "google_service_account_iam_member" "gha_acts_as_build" {
  service_account_id = google_service_account.build.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.gha.member
}

# Тимчасово, доки деплой іде через `gcloud run deploy --source`: Cloud Build
# читає архів джерел із цього bucket. Bucket створив gcloud, тож тут лише доступ.
resource "google_storage_bucket_iam_member" "build_reads_run_sources" {
  bucket = "run-sources-${var.project_id}-${var.region}"
  role   = "roles/storage.objectViewer"
  member = google_service_account.build.member
}

output "run_demo_email" {
  value = google_service_account.run_demo.email
}

output "build_email" {
  value = google_service_account.build.email
}

output "build_id" {
  value = google_service_account.build.id
}

output "gha_id" {
  value = google_service_account.gha.name
}

output "gha_email" {
  value = google_service_account.gha.email
}
