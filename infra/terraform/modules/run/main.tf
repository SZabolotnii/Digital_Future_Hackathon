variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "demo_service_account" {
  type = string
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
    }
  }

  lifecycle {
    # build_config записує `gcloud run deploy --source` під час кожного деплою.
    ignore_changes = [
      template[0].containers[0].image,
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
