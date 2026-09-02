include "root" {
  path = find_in_parent_folders("root.hcl")
}

locals {
  env = read_terragrunt_config(find_in_parent_folders("env.hcl"))
}

terraform {
  source = "${dirname(find_in_parent_folders("root.hcl"))}/modules/app-platform"
}

inputs = {
  project_name = "vinta-schedule"
  environment  = local.env.locals.environment
  aws_region   = local.env.locals.aws_region

  dns_role_arn      = local.env.locals.dns_role_arn
  route53_zone_name = local.env.locals.route53_zone_name
  api_domain        = "api.schedule-staging.vintasoftware.com"

  # Private range for this environment only. Production uses 10.30.0.0/16 so the
  # two could be peered later without renumbering either.
  vpc_cidr = "10.20.0.0/16"

  django_settings_module = "vinta_schedule_api.settings.staging"

  allowed_hosts     = ["api.schedule-staging.vintasoftware.com"]
  site_domain       = "https://schedule-staging.vintasoftware.com"
  frontend_base_url = "https://schedule-staging.vintasoftware.com"

  cors_allowed_origins = [
    "https://schedule-staging.vintasoftware.com",
  ]

  default_from_email = "noreply@schedule-staging.vintasoftware.com"
  default_bcc_emails = ["hugo@vinta.com.br"]

  # Buckets and CloudFront hostnames come from the `storage` stack in this same
  # environment. The bucket names are left to the module's own default, which
  # derives the same <project>-<env>-media / -static names that stack creates.
  media_custom_domain  = "media.schedule-staging.vintasoftware.com"
  static_custom_domain = "static.schedule-staging.vintasoftware.com"

  github_repository = local.env.locals.github_repository
  github_deploy_ref = "refs/heads/main"
  # Empty: staging is the first environment applied in this AWS account, so it
  # creates the account's single GitHub OIDC provider. Production reads its ARN.
  github_oidc_provider_arn = ""

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
