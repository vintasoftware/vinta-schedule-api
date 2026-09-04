# One environment, one Terraform state, one Scalr workspace.
#
# `storage` and `app` used to be separate root modules with a workspace each,
# which meant two Scalr runs per environment and no way for the app to read the
# bucket names the storage stack had actually created -- it re-derived them from
# `<project>-<env>-media` and the two could silently disagree. Composing both
# here removes the ordering rule (storage before app) and the duplication: the
# bucket names and CDN hostnames now flow from one module to the other.
#
# Adding an input: declare it in variables.tf, pass it through below, and make
# sure the child module's variable says `nullable = false`. Optional variables
# here default to `null` so the defaults can stay in the child modules, and
# Terraform only swaps an explicit null for the child's default when the child
# forbids nulls. Without it the null reaches the module body and the plan dies
# on `var.x is null`.

module "storage" {
  source = "../s3-cloudfront"

  providers = {
    aws     = aws
    aws.dns = aws.dns
  }

  project_name = var.project_name
  environment  = var.environment

  route53_zone_name = var.route53_zone_name
  static_domain     = var.static_domain
  media_domain      = var.media_domain

  media_bucket_name  = var.media_bucket_name
  static_bucket_name = var.static_bucket_name

  price_class          = var.cloudfront_price_class
  cors_allowed_origins = var.storage_cors_allowed_origins
}

module "app" {
  source = "../app-platform"

  providers = {
    aws     = aws
    aws.dns = aws.dns
  }

  project_name = var.project_name
  environment  = var.environment
  aws_region   = var.aws_region

  route53_zone_name = var.route53_zone_name
  api_domain        = var.api_domain

  # Buckets and CDN hostnames come from the module above rather than from a
  # second copy of the same values. This is also the dependency edge that makes
  # Terraform create the buckets before the task role that grants access to them.
  media_bucket_name    = module.storage.media_bucket_name
  static_bucket_name   = module.storage.static_bucket_name
  media_custom_domain  = module.storage.media_custom_domain
  static_custom_domain = module.storage.static_custom_domain

  vpc_cidr                = var.vpc_cidr
  availability_zone_count = var.availability_zone_count
  single_nat_gateway      = var.single_nat_gateway

  db_instance_class        = var.db_instance_class
  db_engine_version        = var.db_engine_version
  db_allocated_storage     = var.db_allocated_storage
  db_max_allocated_storage = var.db_max_allocated_storage
  db_multi_az              = var.db_multi_az
  db_backup_retention_days = var.db_backup_retention_days
  db_deletion_protection   = var.db_deletion_protection
  db_name                  = var.db_name
  db_username              = var.db_username

  cache_engine         = var.cache_engine
  cache_engine_version = var.cache_engine_version
  cache_node_type      = var.cache_node_type
  cache_node_count     = var.cache_node_count

  sqs_visibility_timeout_seconds = var.sqs_visibility_timeout_seconds
  sqs_max_receive_count          = var.sqs_max_receive_count

  web_cpu                      = var.web_cpu
  web_memory                   = var.web_memory
  web_desired_count            = var.web_desired_count
  worker_cpu                   = var.worker_cpu
  worker_memory                = var.worker_memory
  worker_desired_count         = var.worker_desired_count
  worker_concurrency           = var.worker_concurrency
  beat_cpu                     = var.beat_cpu
  beat_memory                  = var.beat_memory
  use_fargate_spot_for_workers = var.use_fargate_spot_for_workers
  gunicorn_workers             = var.gunicorn_workers

  container_port            = var.container_port
  health_check_path         = var.health_check_path
  log_retention_days        = var.log_retention_days
  alb_deletion_protection   = var.alb_deletion_protection
  ecr_image_retention_count = var.ecr_image_retention_count

  django_settings_module = var.django_settings_module
  allowed_hosts          = var.allowed_hosts
  site_domain            = var.site_domain
  frontend_base_url      = var.frontend_base_url
  cors_allowed_origins   = var.cors_allowed_origins
  default_from_email     = var.default_from_email
  default_bcc_emails     = var.default_bcc_emails

  account_phone_verification_enabled = var.account_phone_verification_enabled
  default_payment_provider           = var.default_payment_provider
  billing_default_grace_period_days  = var.billing_default_grace_period_days

  extra_environment = var.extra_environment
  extra_secret_keys = var.extra_secret_keys

  github_repository        = var.github_repository
  github_deploy_ref        = var.github_deploy_ref
  github_oidc_provider_arn = var.github_oidc_provider_arn
}
