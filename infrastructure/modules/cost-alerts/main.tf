########################################
# Cost alerts
#
# Two signals, because they catch different things:
#   - A monthly budget: is the month as a whole heading past what we expect?
#     Slow to react (AWS refreshes the numbers a few times a day) but certain.
#   - Cost Anomaly Detection: did one service's spend jump compared with its own
#     history? It catches a runaway resource days before the monthly total
#     would show it.
#
# Both look at the whole AWS account, not just this stack. Production shares the
# account but is not applied yet, so today that is staging's spend; see
# infrastructure/README.md before applying production.
#
# Both are free: AWS charges nothing for the first two budgets or for anomaly
# detection.
########################################

resource "aws_budgets_budget" "monthly" {
  name         = "${var.name_prefix}-account-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Measure usage, not the bill after credits and refunds. With promotional
  # credits on the account, the net cost stays at $0, and an alert on it would
  # never fire however much the infrastructure ran up.
  cost_types {
    include_credit = false
    include_refund = false
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = var.alert_emails
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = var.alert_emails
  }

  # Fires mid-month, as soon as the month's trend points past the budget --
  # usually well before the ACTUAL 100% alert would.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = var.alert_emails
  }
}

resource "aws_ce_anomaly_monitor" "services" {
  count = var.existing_anomaly_monitor_arn == "" ? 1 : 0

  name              = "${var.name_prefix}-services"
  monitor_type      = "DIMENSIONAL"
  monitor_dimension = "SERVICE"
}

locals {
  anomaly_monitor_arn = (
    var.existing_anomaly_monitor_arn != ""
    ? var.existing_anomaly_monitor_arn
    : aws_ce_anomaly_monitor.services[0].arn
  )
}

# Daily digest. Emailing an anomaly immediately needs an SNS topic; a day's
# delay is fine for staging spend.
resource "aws_ce_anomaly_subscription" "email" {
  name             = "${var.name_prefix}-anomalies"
  frequency        = "DAILY"
  monitor_arn_list = [local.anomaly_monitor_arn]

  dynamic "subscriber" {
    for_each = toset(var.alert_emails)

    content {
      type    = "EMAIL"
      address = subscriber.value
    }
  }

  threshold_expression {
    dimension {
      key           = "ANOMALY_TOTAL_IMPACT_ABSOLUTE"
      match_options = ["GREATER_THAN_OR_EQUAL"]
      values        = [tostring(var.anomaly_threshold_usd)]
    }
  }
}
