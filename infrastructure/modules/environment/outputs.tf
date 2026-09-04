# Re-exported verbatim from the two child modules so the names the runbook in
# infrastructure/README.md uses (`terragrunt output cloudfront_key_id`,
# `database_url`, `github_deploy_role_arn`, ...) keep resolving.

########################################
# Storage
########################################

output "media_bucket_name" {
  description = "AWS_MEDIA_BUCKET_NAME"
  value       = module.storage.media_bucket_name
}

output "static_bucket_name" {
  description = "AWS_STATIC_BUCKET_NAME"
  value       = module.storage.static_bucket_name
}

output "media_custom_domain" {
  description = "AWS_MEDIA_S3_CUSTOM_DOMAIN"
  value       = module.storage.media_custom_domain
}

output "static_custom_domain" {
  description = "AWS_STATIC_S3_CUSTOM_DOMAIN"
  value       = module.storage.static_custom_domain
}

output "media_cloudfront_distribution_domain" {
  description = "Underlying *.cloudfront.net domain for the media distribution (debug/DNS)."
  value       = module.storage.media_cloudfront_distribution_domain
}

output "static_cloudfront_distribution_domain" {
  description = "Underlying *.cloudfront.net domain for the static distribution (debug/DNS)."
  value       = module.storage.static_cloudfront_distribution_domain
}

output "media_s3_endpoint_url" {
  description = "AWS_MEDIA_S3_ENDPOINT_URL"
  value       = module.storage.media_s3_endpoint_url
}

output "cloudfront_key_id" {
  description = "AWS_CLOUDFRONT_KEY_ID -- paste into the app secret."
  value       = module.storage.cloudfront_key_id
}

output "cloudfront_private_key" {
  description = "AWS_CLOUDFRONT_KEY (full PEM) -- paste into the app secret."
  value       = module.storage.cloudfront_private_key
  sensitive   = true
}

output "aws_access_key_id" {
  description = "Access key of the storage IAM user. The ECS tasks do not use it -- see the README."
  value       = module.storage.aws_access_key_id
}

output "aws_secret_access_key" {
  description = "Secret of the storage IAM user. The ECS tasks do not use it -- see the README."
  value       = module.storage.aws_secret_access_key
  sensitive   = true
}

########################################
# What CI needs
########################################

output "github_deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN_<ENV> repository variable in GitHub."
  value       = module.app.github_deploy_role_arn
}

output "github_oidc_provider_arn" {
  description = "Pass as `github_oidc_provider_arn` in every other environment in this AWS account -- an account holds only one."
  value       = module.app.github_oidc_provider_arn
}

output "deploy_parameter_name" {
  description = "SSM parameter the deploy script reads for cluster/service/network wiring."
  value       = module.app.deploy_parameter_name
}

output "ecr_repository_url" {
  description = "Image repository the deploy pushes to."
  value       = module.app.ecr_repository_url
}

########################################
# Operations
########################################

output "app_secret_name" {
  description = "Secrets Manager secret holding every app credential. Fill in the empty keys before the first deploy."
  value       = module.app.app_secret_name
}

output "app_secret_arn" {
  description = "ARN of the app secret."
  value       = module.app.app_secret_arn
}

output "ecs_cluster_name" {
  description = "Cluster name, e.g. for `aws ecs execute-command`."
  value       = module.app.ecs_cluster_name
}

output "api_url" {
  description = "Public API URL."
  value       = module.app.api_url
}

output "alb_dns_name" {
  description = "Underlying load balancer hostname (DNS debugging)."
  value       = module.app.alb_dns_name
}

output "celery_queue_url" {
  description = "SQS queue URL backing the Celery broker."
  value       = module.app.celery_queue_url
}

output "celery_dlq_url" {
  description = "Dead-letter queue. A message here is a task that failed sqs_max_receive_count times."
  value       = module.app.celery_dlq_url
}

output "database_endpoint" {
  description = "RDS endpoint (private -- reachable only from inside the VPC)."
  value       = module.app.database_endpoint
}

output "cache_endpoint" {
  description = "ElastiCache primary endpoint (private)."
  value       = module.app.cache_endpoint
}

output "database_url" {
  description = "DATABASE_URL for the app secret. Seeded on the first apply only."
  value       = module.app.database_url
  sensitive   = true
}

output "redis_url" {
  description = "REDIS_URL for the app secret. Seeded on the first apply only."
  value       = module.app.redis_url
  sensitive   = true
}
