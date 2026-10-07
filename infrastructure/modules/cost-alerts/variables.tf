variable "name_prefix" {
  description = "Prefix for the budget and anomaly-detection resource names, e.g. vinta-schedule-staging."
  type        = string
}

variable "alert_emails" {
  description = "Addresses that receive the budget and anomaly emails."
  type        = list(string)

  validation {
    condition     = length(var.alert_emails) > 0
    error_message = "alert_emails needs at least one address; leave the module out instead."
  }
}

variable "monthly_budget_usd" {
  description = <<-DESC
    Monthly spend, in USD, the budget alerts are measured against. Emails go out
    at 80% and 100% of actual spend, and as soon as AWS forecasts the month will
    end above 100%. The budget covers the whole AWS account, not only the
    resources this stack created.
  DESC
  type        = number
  nullable    = false

  validation {
    condition     = var.monthly_budget_usd > 0
    error_message = "monthly_budget_usd must be positive."
  }
}

variable "anomaly_threshold_usd" {
  description = <<-DESC
    Minimum estimated impact, in USD, for a cost anomaly to be emailed. Anomalies
    below it are still recorded in Cost Explorer, just not sent. Small on purpose:
    at ~$1.50/day of normal spend, a $5 anomaly is already a meaningful change.
  DESC
  type        = number
  default     = 5
  nullable    = false
}

variable "existing_anomaly_monitor_arn" {
  description = <<-DESC
    ARN of a per-service anomaly monitor that already exists in the account. AWS
    allows only one per account, and newer accounts are given one automatically,
    so creating a second fails. When set, the subscription uses this monitor and
    the module creates none. Leave it empty to create one.
  DESC
  type        = string
  default     = ""
  nullable    = false
}
