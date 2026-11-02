variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "demo_service_account" {
  type = string
}

# Плейсхолдер: застосунок його не читає. Він відпрацьовує шлях секрету в Cloud Run
# до появи справжніх (пароль БД, тиждень 3). Секрети лише тут, у змінних —
# посилання (NFR-SEC-003).
resource "google_secret_manager_secret" "lmc_config" {
  project   = var.project_id
  secret_id = "LMC_CONFIG"

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
}

resource "google_secret_manager_secret_version" "lmc_config" {
  secret      = google_secret_manager_secret.lmc_config.id
  secret_data = "{}"
}

# Runtime демо читає лише цей секрет.
resource "google_secret_manager_secret_iam_member" "run_demo_reads_lmc_config" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.lmc_config.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.demo_service_account}"
}

# Публічне демо: stub, лише демо-документи, без БД і LLM.
resource "google_cloud_run_v2_service" "demo" {
  project  = var.project_id
  location = var.region
  name     = "livemedcard-demo"
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account                  = var.demo_service_account
    timeout                          = "300s"
    max_instance_request_concurrency = 80

    scaling {
      max_instance_count = 2
    }

    containers {
      # Образ задає деплой, не Terraform (FR-IAC-004).
      image = "us-docker.pkg.dev/cloudrun/container/hello"

      ports {
        name           = "http1"
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1000m"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "LMC_EXTRACTOR"
        value = "stub"
      }
      env {
        name  = "LMC_IMAGE_OCR"
        value = "none"
      }
      env {
        name  = "LMC_SEED_SAMPLES"
        value = "1"
      }
      env {
        name = "LMC_CONFIG"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.lmc_config.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  # Без доступу до секрету ревізія не стартує.
  depends_on = [
    google_secret_manager_secret_iam_member.run_demo_reads_lmc_config,
    google_secret_manager_secret_version.lmc_config,
  ]

  lifecycle {
    # Образ і трафік задає деплой (cloudbuild.yaml); build_config записував
    # `gcloud run deploy --source`. Ім'я ревізії деплой у шаблон не пише.
    ignore_changes = [
      template[0].containers[0].image,
      traffic,
      build_config,
      client,
      client_version,
    ]
  }
}

resource "google_cloud_run_v2_service_iam_member" "demo_public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.demo.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "demo_url" {
  value = google_cloud_run_v2_service.demo.uri
}
