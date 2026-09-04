include "root" {
  path = find_in_parent_folders("root.hcl")
}

locals {
  env = read_terragrunt_config("${get_terragrunt_dir()}/env.hcl")
}

# One stack for the whole environment: buckets + CDN and the runtime platform,
# in one state and one Scalr run. Set the Scalr workspace's Working Directory to
# this folder.
# The `//` is load-bearing: terragrunt copies everything before it into its
# cache and then works in the subdirectory after it. Without it only
# `modules/environment` is copied, and its `../app-platform` /
# `../s3-cloudfront` module sources resolve to nothing.
terraform {
  source = "${dirname(find_in_parent_folders("root.hcl"))}/modules//environment"
}

# Not applied yet. Read "Before applying production" in infrastructure/README.md
# first; in particular `github_oidc_provider_arn` must be filled in from the
# staging output, because an AWS account holds exactly one GitHub OIDC provider.
inputs = {
  project_name = "vinta-schedule"
  environment  = local.env.locals.environment
  aws_region   = local.env.locals.aws_region

  dns_role_arn      = local.env.locals.dns_role_arn
  route53_zone_name = local.env.locals.route53_zone_name

  ####################################
  # Domains
  ####################################

  api_domain    = "api.schedule.vintasoftware.com"
  media_domain  = "media.schedule.vintasoftware.com"
  static_domain = "static.schedule.vintasoftware.com"

  ####################################
  # Network
  ####################################

  vpc_cidr = "10.30.0.0/16"

  ####################################
  # Django
  ####################################

  django_settings_module = "vinta_schedule_api.settings.production"

  allowed_hosts     = ["api.schedule.vintasoftware.com"]
  site_domain       = "https://schedule.vintasoftware.com"
  frontend_base_url = "https://schedule.vintasoftware.com"

  # The API's own CORS headers.
  cors_allowed_origins = [
    "https://schedule.vintasoftware.com",
  ]

  # Direct browser uploads to the media bucket (django-s3direct). Lock these to
  # the real frontend before going live.
  storage_cors_allowed_origins = [
    "https://schedule.vintasoftware.com",
  ]

  default_from_email = "noreply@schedule.vintasoftware.com"
  default_bcc_emails = []

  ####################################
  # CI
  ####################################

  github_repository = local.env.locals.github_repository
  github_deploy_ref = "refs/heads/main"
  # Fill in from `terragrunt output github_oidc_provider_arn` in the staging
  # environment. Leaving it empty makes this apply fail on a duplicate provider.
  github_oidc_provider_arn = ""

  ####################################
  # Sizing
  ####################################

  # Two web tasks so a rolling deploy never drops to zero capacity, and a database
  # that survives losing an AZ.
  web_desired_count    = 2
  worker_desired_count = 1
  worker_concurrency   = 4

  web_cpu       = 1024
  web_memory    = 2048
  worker_cpu    = 512
  worker_memory = 2048

  db_instance_class        = "db.t4g.small"
  db_multi_az              = true
  db_deletion_protection   = true
  db_backup_retention_days = 14

  cache_node_type  = "cache.t4g.micro"
  cache_node_count = 1

  alb_deletion_protection = true
}
