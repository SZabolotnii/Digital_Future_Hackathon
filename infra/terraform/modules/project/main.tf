variable "project_id" {
  type = string
}

variable "budget_usd" {
  type = number
}

variable "services" {
  type = list(string)
}

variable "billing_account" {
  type = string
}

resource "google_project_service" "this" {
  for_each = toset(var.services)

  project         = var.project_id
  service         = each.value
  deletion_policy = "ABANDON"
}

data "google_project" "this" {
  project_id = var.project_id
}

# FR-IAC-006. Кредити trial виключено, інакше алерт не спрацює до кінця trial.
resource "google_billing_budget" "monthly" {
  billing_account = var.billing_account
  display_name    = "apps-deploy monthly ${var.budget_usd}"

  budget_filter {
    projects               = ["projects/${data.google_project.this.number}"]
    credit_types_treatment = "EXCLUDE_ALL_CREDITS"
    calendar_period        = "MONTH"
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.budget_usd)
    }
  }

  dynamic "threshold_rules" {
    for_each = [0.5, 0.9, 1.0]
    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "CURRENT_SPEND"
    }
  }
}
