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

  # A NAT instance instead of the managed gateway (~$7/month against ~$36). Staging's
  # outbound traffic is a trickle of calendar/payment API calls, and an hour of
  # it being down is an inconvenience, not an incident.
  nat_mode = "instance"

  ####################################
  # Ingress
  ####################################

  # API Gateway instead of an ALB: billed per request rather than ~$24/month
  # fixed. Its limits (30s timeout, no WAF, no draining on deploys) are listed in
  # modules/app-platform/api_gateway.tf. Production stays on the ALB.
  ingress_mode = "api_gateway"

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

  # Staging takes payments through Stripe, so the MercadoPago credentials are
  # not carried here. Every key the task definitions name has to exist in the
  # app secret or ECS starts no container at all, and settings/base.py reads
  # these three with a default -- absent behaves exactly like the empty string.
  disabled_secret_keys = [
    "MERCADOPAGO_ACCESS_TOKEN",
    "MERCADOPAGO_WEBHOOK_SECRET",
    "MERCADOPAGO_PUBLIC_KEY",
  ]

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

  # Every task on Spot, and the beat scheduler inside the worker rather than a
  # task of its own. A Spot reclamation of the web task is a short outage, which
  # staging can take.
  use_fargate_spot_for_web = true
  run_beat_in_worker       = true

  db_instance_class      = "db.t4g.micro"
  db_deletion_protection = false

  cache_node_type  = "cache.t4g.micro"
  cache_node_count = 1

  ####################################
  # Cost alerts
  ####################################

  # These watch the whole AWS account, which production shares. That is fine
  # while production is unapplied; revisit before applying it (see "Before
  # applying production" in infrastructure/README.md). Staging's normal spend is
  # ~$45/month.
  cost_alert_emails = [
    "hugo@vinta.com.br",
    "flavio@vinta.com.br",
    "felipe@vinta.com.br",
  ]
  monthly_budget_usd = 50
}
