########################################
# Identity
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
  description = "Region every resource in this module is created in. Also the region the containers get as AWS_REGION."
  type        = string
}

########################################
# Network
########################################

variable "vpc_cidr" {
  description = "CIDR for the VPC. Must not overlap any other VPC you intend to peer with."
  type        = string
}

variable "availability_zone_count" {
  description = <<-DESC
    How many AZs to spread subnets across. Two is the floor: RDS subnet groups and
    ALBs both refuse a single AZ. Raising it adds subnets, not NAT gateways -- see
    `single_nat_gateway`.
  DESC
  type        = number
  default     = 2
  nullable    = false

  validation {
    condition     = var.availability_zone_count >= 2
    error_message = "availability_zone_count must be at least 2 (ALB and RDS both require two AZs)."
  }
}

variable "single_nat_gateway" {
  description = <<-DESC
    Route every private subnet through one NAT gateway in the first AZ. A NAT
    gateway is ~$32/month plus data, so one shared gateway is the cost-minimal
    choice; the tradeoff is that losing that AZ cuts outbound internet (external
    calendar/payment APIs) for tasks in every AZ. Set false for one per AZ.
  DESC
  type        = bool
  default     = true
  nullable    = false
}

########################################
# DNS / TLS
########################################

variable "route53_zone_name" {
  description = "Route 53 hosted zone the API domain lives under (e.g. vintasoftware.com). No trailing dot."
  type        = string
}

variable "api_domain" {
  description = "Public hostname for the API, pointed at the ALB (e.g. api.schedule-staging.vintasoftware.com)."
  type        = string
}

########################################
# Database (RDS Postgres)
########################################

variable "db_instance_class" {
  description = "RDS instance class. db.t4g.micro is the smallest Graviton burstable and the cheapest option that still runs this schema."
  type        = string
  default     = "db.t4g.micro"
  nullable    = false
}

variable "db_engine_version" {
  description = "Postgres major version. Major-only lets AWS pick the current minor; auto_minor_version_upgrade keeps it patched."
  type        = string
  default     = "17"
  nullable    = false
}

variable "db_allocated_storage" {
  description = "Initial gp3 storage in GB. Storage autoscaling grows it up to db_max_allocated_storage."
  type        = number
  default     = 20
  nullable    = false
}

variable "db_max_allocated_storage" {
  description = "Ceiling for RDS storage autoscaling, in GB."
  type        = number
  default     = 100
  nullable    = false
}

variable "db_multi_az" {
  description = "Multi-AZ doubles the instance cost. Off by default; turn it on for production once the data matters."
  type        = bool
  default     = false
  nullable    = false
}

variable "db_backup_retention_days" {
  description = "Automated backup retention. 0 disables backups (and point-in-time recovery)."
  type        = number
  default     = 7
  nullable    = false
}

variable "db_deletion_protection" {
  description = "Block `terraform destroy` (and console deletion) of the database."
  type        = bool
  default     = false
  nullable    = false
}

variable "db_name" {
  description = "Initial database name."
  type        = string
  default     = "vinta_schedule_api"
  nullable    = false
}

variable "db_username" {
  description = "Master username. `postgres` and `admin` are reserved by RDS."
  type        = string
  default     = "vinta_schedule_api"
  nullable    = false
}

########################################
# Cache (ElastiCache)
########################################

variable "cache_engine" {
  description = <<-DESC
    `valkey` or `redis`. Valkey is wire-compatible (redis-py, celery, redbeat and
    django-defender all speak to it unchanged) and AWS prices its node hours below
    Redis OSS, so it is the default. Switch to `redis` if you hit a compatibility
    surprise -- the connection URL does not change.
  DESC
  type        = string
  default     = "valkey"
  nullable    = false

  validation {
    condition     = contains(["valkey", "redis"], var.cache_engine)
    error_message = "cache_engine must be either \"valkey\" or \"redis\"."
  }
}

variable "cache_engine_version" {
  description = "Engine version for the cache. Must exist for the chosen engine (valkey 8.x / redis 7.x)."
  type        = string
  default     = "8.0"
  nullable    = false
}

variable "cache_node_type" {
  description = "ElastiCache node type. cache.t4g.micro is the smallest node AWS sells."
  type        = string
  default     = "cache.t4g.micro"
  nullable    = false
}

variable "cache_node_count" {
  description = "Nodes in the replication group. 1 = primary only, no failover, cheapest."
  type        = number
  default     = 1
  nullable    = false
}

########################################
# Broker (SQS)
########################################

variable "sqs_visibility_timeout_seconds" {
  description = <<-DESC
    How long a task stays invisible to other workers after being picked up. Must
    exceed the longest task's runtime, because CELERY_TASK_ACKS_LATE is on: the
    message is only deleted once the task finishes, and a timeout that expires
    first hands the same task to a second worker.
  DESC
  type        = number
  default     = 900
  nullable    = false
}

variable "sqs_max_receive_count" {
  description = "Deliveries before a message is moved to the dead-letter queue. Guards against a poison task looping forever."
  type        = number
  default     = 5
  nullable    = false
}

########################################
# ECS services
########################################

variable "web_cpu" {
  description = "Fargate CPU units for the web task (1024 = 1 vCPU)."
  type        = number
  default     = 512
  nullable    = false
}

variable "web_memory" {
  description = "Fargate memory (MiB) for the web task. Must be a pairing Fargate allows for web_cpu."
  type        = number
  default     = 1024
  nullable    = false
}

variable "web_desired_count" {
  description = "Web tasks to run. 1 means a deploy or task replacement is a brief gap; 2 makes rolling deploys seamless at double the cost."
  type        = number
  default     = 1
  nullable    = false
}

variable "worker_cpu" {
  description = "Fargate CPU units for the Celery worker task."
  type        = number
  default     = 256
  nullable    = false
}

variable "worker_memory" {
  description = "Fargate memory (MiB) for the Celery worker task."
  type        = number
  default     = 1024
  nullable    = false
}

variable "worker_desired_count" {
  description = "Celery worker tasks to run."
  type        = number
  default     = 1
  nullable    = false
}

variable "worker_concurrency" {
  description = "Celery prefork child processes per worker task. Each child is a full Django process -- keep worker_memory ahead of it."
  type        = number
  default     = 2
  nullable    = false
}

variable "beat_cpu" {
  description = "Fargate CPU units for the Celery beat task."
  type        = number
  default     = 256
  nullable    = false
}

variable "beat_memory" {
  description = "Fargate memory (MiB) for the Celery beat task."
  type        = number
  default     = 512
  nullable    = false
}

variable "use_fargate_spot_for_workers" {
  description = <<-DESC
    Run worker and beat on FARGATE_SPOT (roughly 70% of on-demand price). Safe for
    both: the worker acks late so an interrupted task returns to SQS after the
    visibility timeout, and beat holds a redbeat lock so a replacement instance
    picks the schedule back up. The web service always stays on on-demand FARGATE.
  DESC
  type        = bool
  default     = true
  nullable    = false
}

variable "gunicorn_workers" {
  description = "Gunicorn worker processes in the web container."
  type        = number
  default     = 2
  nullable    = false
}

variable "container_port" {
  description = "Port gunicorn binds inside the container; also the ALB target port."
  type        = number
  default     = 8000
  nullable    = false
}

variable "health_check_path" {
  description = "Path the ALB target group polls. Must be exempt from SECURE_SSL_REDIRECT (see settings/production.py)."
  type        = string
  default     = "/healthz/"
  nullable    = false
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for every service log group."
  type        = number
  default     = 14
  nullable    = false
}

variable "alb_deletion_protection" {
  description = "Block deletion of the load balancer."
  type        = bool
  default     = false
  nullable    = false
}

variable "ecr_image_retention_count" {
  description = "Tagged images to keep in ECR before the lifecycle policy expires the oldest."
  type        = number
  default     = 20
  nullable    = false
}

########################################
# Application configuration
#
# Everything here lands in the task definitions as plain `environment` entries.
# Secrets never appear in this list -- they live in the Secrets Manager secret
# this module creates (see secrets.tf).
########################################

variable "django_settings_module" {
  description = "DJANGO_SETTINGS_MODULE for the deployed containers."
  type        = string
}

variable "allowed_hosts" {
  description = "ALLOWED_HOSTS entries. The ALB health check's own target IP is appended at runtime from the ECS task metadata, so it does not belong here."
  type        = list(string)
}

variable "site_domain" {
  description = "SITE_DOMAIN -- the frontend's public origin."
  type        = string
}

variable "frontend_base_url" {
  description = "FRONTEND_BASE_URL -- base for account/email links."
  type        = string
}

variable "cors_allowed_origins" {
  description = "CORS_ALLOWED_ORIGINS for the API."
  type        = list(string)
}

variable "default_from_email" {
  description = "DEFAULT_FROM_EMAIL. Must be on a domain the SMTP provider is verified to send for."
  type        = string
}

variable "default_bcc_emails" {
  description = "DEFAULT_BCC_EMAILS."
  type        = list(string)
  default     = []
  nullable    = false
}

variable "media_bucket_name" {
  description = "Media bucket from the storage stack. Defaults to that stack's own naming: <project>-<env>-media."
  type        = string
  default     = ""
  nullable    = false
}

variable "static_bucket_name" {
  description = "Static bucket from the storage stack. Defaults to that stack's own naming: <project>-<env>-static."
  type        = string
  default     = ""
  nullable    = false
}

variable "media_custom_domain" {
  description = "AWS_MEDIA_S3_CUSTOM_DOMAIN -- the media CloudFront hostname from the storage stack."
  type        = string
}

variable "static_custom_domain" {
  description = "AWS_STATIC_S3_CUSTOM_DOMAIN -- the static CloudFront hostname from the storage stack."
  type        = string
}

variable "account_phone_verification_enabled" {
  description = "ACCOUNT_PHONE_VERIFICATION_ENABLED rollout gate."
  type        = bool
  default     = false
  nullable    = false
}

variable "default_payment_provider" {
  description = "DEFAULT_PAYMENT_PROVIDER (stripe or mercadopago)."
  type        = string
  default     = "stripe"
  nullable    = false
}

variable "billing_default_grace_period_days" {
  description = "BILLING_DEFAULT_GRACE_PERIOD_DAYS fallback dunning window."
  type        = number
  default     = 7
  nullable    = false
}

variable "extra_environment" {
  description = "Additional non-secret env vars merged into every container. Later keys win over the module's own."
  type        = map(string)
  default     = {}
  nullable    = false
}

variable "disabled_secret_keys" {
  description = <<-EOT
    Optional credentials this environment does not use, e.g. the MERCADOPAGO_*
    trio where payments run through Stripe.

    Every key the task definitions name must exist in the app secret -- ECS
    fails the WHOLE task, not just that variable, when one is missing. Dropping
    a key here removes it from both the seed and the task definitions, and
    settings/base.py reads all of these with a default, so the absent variable
    resolves to the same "" an empty entry would have given.

    Only the optional set can be dropped. Naming a key Django reads without a
    default (SMTP_*, TWILIO_ACCOUNT_SID, TWILIO_NUMBER, AWS_CLOUDFRONT_KEY*)
    has no effect -- see `local.required_secret_keys`.
  EOT
  type        = list(string)
  default     = []
  nullable    = false
}

variable "extra_secret_keys" {
  description = "Extra keys to seed (empty) in the Secrets Manager secret and inject as container secrets. For env vars added to the app after this module was written."
  type        = list(string)
  default     = []
  nullable    = false
}

########################################
# CI / GitHub Actions
########################################

variable "github_repository" {
  description = "owner/name of the repository allowed to assume the deploy role over OIDC."
  type        = string
}

variable "github_deploy_ref" {
  description = "Git ref whose workflow runs may assume the deploy role. Scoping this to one branch is what stops a pull request from a fork deploying."
  type        = string
  default     = "refs/heads/main"
  nullable    = false
}

variable "github_oidc_provider_arn" {
  description = <<-DESC
    ARN of an existing GitHub OIDC provider. An AWS account holds exactly one, so
    only the first environment applied in an account creates it -- leave this empty
    there, and set it to that provider's ARN in every other environment.
  DESC
  type        = string
  default     = ""
  nullable    = false
}
