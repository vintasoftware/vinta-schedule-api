########################################
# What CI needs
########################################

output "github_deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN repository variable in GitHub."
  value       = aws_iam_role.github_deploy.arn
}

output "github_oidc_provider_arn" {
  description = "Pass this as `github_oidc_provider_arn` in every other environment in this AWS account -- an account holds only one."
  value       = local.github_oidc_provider_arn
}

output "deploy_parameter_name" {
  description = "SSM parameter the deploy script reads for cluster/service/network wiring."
  value       = aws_ssm_parameter.deploy.name
}

output "ecr_repository_url" {
  description = "Image repository the deploy pushes to."
  value       = aws_ecr_repository.app.repository_url
}

########################################
# Operations
########################################

output "app_secret_name" {
  description = "Secrets Manager secret holding every app credential. Fill in the empty keys before the first deploy."
  value       = aws_secretsmanager_secret.app.name
}

output "app_secret_arn" {
  description = "ARN of the app secret."
  value       = aws_secretsmanager_secret.app.arn
}

output "ecs_cluster_name" {
  description = "Cluster name, e.g. for `aws ecs execute-command`."
  value       = aws_ecs_cluster.this.name
}

output "api_url" {
  description = "Public API URL."
  value       = "https://${var.api_domain}"
}

output "alb_dns_name" {
  description = "Underlying load balancer hostname (DNS debugging). Null when ingress_mode = \"api_gateway\"."
  value       = one(aws_lb.this[*].dns_name)
}

output "api_gateway_target_domain_name" {
  description = "Underlying API Gateway regional hostname the API domain aliases (DNS debugging). Null when ingress_mode = \"alb\"."
  value       = one(aws_apigatewayv2_domain_name.api[*].domain_name_configuration[0].target_domain_name)
}

output "nat_public_ip" {
  description = "Address outbound traffic from the tasks leaves from -- what a third party would allowlist."
  value = (
    var.nat_mode == "instance"
    ? aws_eip.nat_instance[0].public_ip
    : aws_eip.nat[0].public_ip
  )
}

output "celery_queue_url" {
  description = "SQS queue URL backing the Celery broker."
  value       = aws_sqs_queue.celery.url
}

output "celery_dlq_url" {
  description = "Dead-letter queue. A message here is a task that failed sqs_max_receive_count times."
  value       = aws_sqs_queue.celery_dlq.url
}

output "database_endpoint" {
  description = "RDS endpoint (private -- reachable only from inside the VPC)."
  value       = aws_db_instance.this.address
}

output "cache_endpoint" {
  description = "ElastiCache primary endpoint (private)."
  value       = aws_elasticache_replication_group.this.primary_endpoint_address
}

########################################
# Regenerated credentials
#
# Seeded into the app secret on the first apply only -- `ignore_changes` means a
# later change to either of these has to be pasted into the secret by hand.
########################################

output "database_url" {
  description = "DATABASE_URL for the app secret."
  value       = local.database_url
  sensitive   = true
}

output "redis_url" {
  description = "REDIS_URL for the app secret."
  value       = local.redis_url
  sensitive   = true
}
