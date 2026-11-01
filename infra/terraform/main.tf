provider "google" {
  project = var.project_id
  region  = var.region

  # Billing Budget API вимагає quota project для користувацьких облікових даних.
  user_project_override = true
  billing_project       = var.project_id
}

# FR-IAC-003: API вмикаються в тому спринті, де вперше потрібні.
locals {
  services = [
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "billingbudgets.googleapis.com",
    "cloudbilling.googleapis.com",
  ]
}

data "google_billing_account" "this" {
  display_name = var.billing_account_name
  open         = true
}

module "project" {
  source     = "./modules/project"
  project_id = var.project_id
  budget_usd = var.budget_usd
  services   = local.services

  billing_account = data.google_billing_account.this.id
}

module "registry" {
  source     = "./modules/registry"
  project_id = var.project_id
  region     = var.region

  depends_on = [module.project]
}

module "iam" {
  source     = "./modules/iam"
  project_id = var.project_id
  region     = var.region

  depends_on = [module.project]
}

module "run" {
  source               = "./modules/run"
  project_id           = var.project_id
  region               = var.region
  demo_service_account = module.iam.run_demo_email
}

module "cicd" {
  source                 = "./modules/cicd"
  project_id             = var.project_id
  github_repository      = var.github_repository
  github_repository_id   = var.github_repository_id
  gha_service_account_id = module.iam.gha_id

  region                   = var.region
  build_service_account_id = module.iam.build_id

  depends_on = [module.project]
}
