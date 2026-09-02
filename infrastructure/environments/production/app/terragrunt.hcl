include "root" {
  path = find_in_parent_folders("root.hcl")
}

locals {
  env = read_terragrunt_config(find_in_parent_folders("env.hcl"))
}

terraform {
  source = "${dirname(find_in_parent_folders("root.hcl"))}/modules/app-platform"
}

# Not applied yet -- mirrors the unapplied production `storage` stack. Read the
# NOT-YET-APPLIED notes in infrastructure/README.md before the first apply; in
# particular `github_oidc_provider_arn` must be filled in from the staging stack's
# output, because an AWS account holds exactly one GitHub OIDC provider.
inputs = {
  project_name = "vinta-schedule"
  environment  = local.env.locals.environment
  aws_region   = local.env.locals.aws_region

  dns_role_arn      = local.env.locals.dns_role_arn
  route53_zone_name = local.env.locals.route53_zone_name
  api_domain        = "api.schedule.vintasoftware.com"

  vpc_cidr = "10.30.0.0/16"

  django_settings_module = "vinta_schedule_api.settings.production"

  allowed_hosts     = ["api.schedule.vintasoftware.com"]
  site_domain       = "https://schedule.vintasoftware.com"
  frontend_base_url = "https://schedule.vintasoftware.com"

  cors_allowed_origins = [
    "https://schedule.vintasoftware.com",
  ]

  default_from_email = "noreply@schedule.vintasoftware.com"
  default_bcc_emails = []

  media_custom_domain  = "media.schedule.vintasoftware.com"
  static_custom_domain = "static.schedule.vintasoftware.com"

  github_repository = local.env.locals.github_repository
  github_deploy_ref = "refs/heads/main"
  # Fill in from `terragrunt output github_oidc_provider_arn` in the staging app
  # stack. Leaving it empty makes this apply fail on a duplicate OIDC provider.
  github_oidc_provider_arn = ""

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
