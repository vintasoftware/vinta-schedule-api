include "root" {
  path = find_in_parent_folders("root.hcl")
}

locals {
  env = read_terragrunt_config("${get_terragrunt_dir()}/env.hcl")
}

# One stack for the whole environment: buckets + CDN and the runtime platform,
# in one state and one Scalr run. Set the Scalr workspace's Working Directory to
# this folder.
terraform {
  source = "${dirname(find_in_parent_folders("root.hcl"))}/modules/environment"
}

inputs = {
  project_name = "vinta-schedule"
  environment  = local.env.locals.environment
  aws_region   = local.env.locals.aws_region

  dns_role_arn      = local.env.locals.dns_role_arn
  route53_zone_name = local.env.locals.route53_zone_name

  ####################################
  # Domains
  ####################################

  api_domain    = "api.schedule-staging.vintasoftware.com"
  media_domain  = "media.schedule-staging.vintasoftware.com"
  static_domain = "static.schedule-staging.vintasoftware.com"

  ####################################
  # Network
  ####################################

  # Private range for this environment only. Production uses 10.30.0.0/16 so the
  # two could be peered later without renumbering either.
  vpc_cidr = "10.20.0.0/16"

  ####################################
  # Django
  ####################################

  django_settings_module = "vinta_schedule_api.settings.staging"

  allowed_hosts     = ["api.schedule-staging.vintasoftware.com"]
  site_domain       = "https://schedule-staging.vintasoftware.com"
  frontend_base_url = "https://schedule-staging.vintasoftware.com"

  # The API's own CORS headers.
  cors_allowed_origins = [
    "https://schedule-staging.vintasoftware.com",
  ]

  # Direct browser uploads to the media bucket (django-s3direct). Separate knob.
  storage_cors_allowed_origins = [
    "https://schedule-staging.vintasoftware.com",
  ]

  default_from_email = "noreply@schedule-staging.vintasoftware.com"
  default_bcc_emails = ["hugo@vinta.com.br"]

  ####################################
  # CI
  ####################################

  github_repository = local.env.locals.github_repository
  github_deploy_ref = "refs/heads/main"
  # Empty: staging is the first environment applied in this AWS account, so it
  # creates the account's single GitHub OIDC provider. Production reads its ARN.
  github_oidc_provider_arn = ""

  ####################################
  # Sizing
  ####################################

  # Staging is a single-user-load environment -- one task each, and the smallest
  # database and cache nodes AWS sells.
  web_desired_count    = 1
  worker_desired_count = 1
  worker_concurrency   = 2

  db_instance_class      = "db.t4g.micro"
  db_deletion_protection = false

  cache_node_type  = "cache.t4g.micro"
  cache_node_count = 1
}
