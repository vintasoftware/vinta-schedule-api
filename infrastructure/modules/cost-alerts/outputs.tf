output "budget_name" {
  description = "Name of the monthly budget, as it appears under Billing → Budgets."
  value       = aws_budgets_budget.monthly.name
}

output "anomaly_monitor_arn" {
  description = "Anomaly monitor the email subscription is attached to."
  value       = local.anomaly_monitor_arn
}
