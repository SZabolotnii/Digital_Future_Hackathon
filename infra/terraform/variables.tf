variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "budget_usd" {
  type        = number
  description = "Місячний бюджет проєкту в USD; алерти на 50/90/100 % фактичних витрат (FR-IAC-006)."
}

variable "billing_account_name" {
  type        = string
  description = "Назва billing-акаунта; ID у репо не тримаємо."
}

variable "github_repository" {
  type = string
}

variable "github_repository_id" {
  type = string
}
