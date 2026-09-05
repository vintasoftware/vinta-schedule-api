# Pass-through inputs for the two child modules.
#
# Every optional variable defaults to `null`, and every child variable with a
# default is declared `nullable = false` -- which is what makes Terraform
# substitute that default when this module passes a null. So the defaults and
# their documentation live in modules/s3-cloudfront and modules/app-platform
# and are not duplicated here. Drop the `nullable = false` on the child side
# and the null arrives verbatim instead.

########################################
# Identity + providers
#
# `aws_region` and `dns_role_arn` are read by the provider block root.hcl
# generates into this module, as well as (for the region) by the app module.
########################################

variable "project_name" {
  description = "Project slug used to derive resource names."
  type        = string
}

variable "environment" {
  description = "Environment slug (e.g. production, staging)."
  type        = string
}

variable "aws_region" {
  description = "Region every resource is created in."
  type        = string
}

variable "dns_role_arn" {
  description = "Role in the DNS account that the aws.dns provider assumes to write Route 53 records."
  type        = string
}

variable "route53_zone_name" {
  description = "Route 53 hosted zone the custom domains live under (e.g. vintasoftware.com). No trailing dot."
  type        = string
}

########################################
# Storage (modules/s3-cloudfront)
########################################

variable "media_domain" {
  description = "Custom domain for the media CloudFront distribution."
  type        = string
}

variable "static_domain" {
  description = "Custom domain for the static CloudFront distribution."
  type        = string
}

variable "media_bucket_name" {
  description = "Explicit media bucket name. Derived from <project>-<env>-media when unset."
  type        = string
  default     = null
}

variable "static_bucket_name" {
  description = "Explicit static bucket name. Derived from <project>-<env>-static when unset."
  type        = string
  default     = null
}

variable "cloudfront_price_class" {
  description = "CloudFront price class for both distributions."
  type        = string
  default     = null
}

variable "storage_cors_allowed_origins" {
  description = "Origins allowed to upload directly to the media bucket (django-s3direct). Separate from the API's own CORS list on purpose."
  type        = list(string)
  default     = null
}

########################################
# Network (modules/app-platform)
########################################

variable "vpc_cidr" {
  description = "CIDR for this environment's VPC."
  type        = string
}

variable "api_domain" {
  description = "Public hostname the ALB answers on."
  type        = string
}

variable "availability_zone_count" {
  type    = number
  default = null
}

variable "single_nat_gateway" {
  type    = bool
  default = null
}

########################################
# Database
########################################

variable "db_instance_class" {
  type    = string
  default = null
}

variable "db_engine_version" {
  type    = string
  default = null
}

variable "db_allocated_storage" {
  type    = number
  default = null
}

variable "db_max_allocated_storage" {
  type    = number
  default = null
}

variable "db_multi_az" {
  type    = bool
  default = null
}

variable "db_backup_retention_days" {
  type    = number
  default = null
}

variable "db_deletion_protection" {
  type    = bool
  default = null
}

variable "db_name" {
  type    = string
  default = null
}

variable "db_username" {
  type    = string
  default = null
}

########################################
# Cache
########################################

variable "cache_engine" {
  type    = string
  default = null
}

variable "cache_engine_version" {
  type    = string
  default = null
}

variable "cache_node_type" {
  type    = string
  default = null
}

variable "cache_node_count" {
  type    = number
  default = null
}

########################################
# Queue
########################################

variable "sqs_visibility_timeout_seconds" {
  type    = number
  default = null
}

variable "sqs_max_receive_count" {
  type    = number
  default = null
}

########################################
# Services
########################################

variable "web_cpu" {
  type    = number
  default = null
}

variable "web_memory" {
  type    = number
  default = null
}

variable "web_desired_count" {
  type    = number
  default = null
}

variable "worker_cpu" {
  type    = number
  default = null
}

variable "worker_memory" {
  type    = number
  default = null
}

variable "worker_desired_count" {
  type    = number
  default = null
}

variable "worker_concurrency" {
  type    = number
  default = null
}

variable "beat_cpu" {
  type    = number
  default = null
}

variable "beat_memory" {
  type    = number
  default = null
}

variable "use_fargate_spot_for_workers" {
  type    = bool
  default = null
}

variable "gunicorn_workers" {
  type    = number
  default = null
}

variable "container_port" {
  type    = number
  default = null
}

variable "health_check_path" {
  type    = string
  default = null
}

variable "log_retention_days" {
  type    = number
  default = null
}

variable "alb_deletion_protection" {
  type    = bool
  default = null
}

variable "ecr_image_retention_count" {
  type    = number
  default = null
}

########################################
# Django settings the containers read
########################################

variable "django_settings_module" {
  description = "DJANGO_SETTINGS_MODULE for every container."
  type        = string
}

variable "allowed_hosts" {
  description = "ALLOWED_HOSTS."
  type        = list(string)
}

variable "site_domain" {
  description = "SITE_DOMAIN / absolute-URL base."
  type        = string
}

variable "frontend_base_url" {
  description = "FRONTEND_BASE_URL used in emails and OAuth redirects."
  type        = string
}

variable "cors_allowed_origins" {
  description = "Origins the API answers CORS requests from. Separate from storage_cors_allowed_origins on purpose."
  type        = list(string)
}

variable "default_from_email" {
  description = "DEFAULT_FROM_EMAIL."
  type        = string
}

variable "default_bcc_emails" {
  type    = list(string)
  default = null
}

variable "account_phone_verification_enabled" {
  type    = bool
  default = null
}

variable "default_payment_provider" {
  type    = string
  default = null
}

variable "billing_default_grace_period_days" {
  type    = number
  default = null
}

variable "extra_environment" {
  description = "Extra plain environment variables for every container."
  type        = map(string)
  default     = null
}

variable "extra_secret_keys" {
  description = "Extra keys to seed empty in the app secret and inject into the containers."
  type        = list(string)
  default     = null
}

variable "disabled_secret_keys" {
  description = "Optional credentials this environment does not use. See the app-platform variable of the same name."
  type        = list(string)
  default     = null
}

########################################
# CI
########################################

variable "github_repository" {
  description = "Repository whose workflow runs may assume the ECS deploy role."
  type        = string
}

variable "github_deploy_ref" {
  type    = string
  default = null
}

variable "github_oidc_provider_arn" {
  description = "ARN of the account's single GitHub OIDC provider. Empty means create it (only one environment per AWS account may do so)."
  type        = string
  default     = null
}
