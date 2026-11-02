variable "project_id" {
  type = string
}

variable "demo_url" {
  type = string
}

variable "alert_email" {
  type = string
}

resource "google_monitoring_notification_channel" "email" {
  project      = var.project_id
  display_name = "LiveMedCard alerts (email)"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
}

# FR-OBS-003: GET /health демо раз на 5 хв.
resource "google_monitoring_uptime_check_config" "demo_health" {
  project      = var.project_id
  display_name = "livemedcard-demo /health"
  period       = "300s"
  timeout      = "10s"

  http_check {
    path         = "/health"
    port         = 443
    use_ssl      = true
    validate_ssl = true
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = trimprefix(var.demo_url, "https://")
    }
  }

  content_matchers {
    content = "\"status\":\"ok\""
    matcher = "CONTAINS_STRING"
  }
}

# Алерт, коли перевірка падає більш ніж з одного регіону довше 10 хв.
resource "google_monitoring_alert_policy" "demo_down" {
  project      = var.project_id
  display_name = "livemedcard-demo /health fails"
  combiner     = "OR"

  conditions {
    display_name = "Uptime check failed"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.demo_health.uptime_check_id}\" AND resource.type=\"uptime_url\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "600s"

      aggregations {
        alignment_period     = "1200s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.project_id", "resource.label.host"]
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]
}
