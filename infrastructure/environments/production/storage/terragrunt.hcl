include "root" {
  path = find_in_parent_folders("terragrunt.hcl")
}

locals {
  env = read_terragrunt_config(find_in_parent_folders("env.hcl"))
}

terraform {
  source = "${get_repo_root()}/infrastructure/modules/s3-cloudfront"
}

inputs = {
  project_name = "vinta-schedule"
  environment  = local.env.locals.environment
  aws_region   = local.env.locals.aws_region

  dns_role_arn      = local.env.locals.dns_role_arn
  route53_zone_name = local.env.locals.route53_zone_name
  static_domain     = "static.schedule.vintasoftware.com"
  media_domain      = "media.schedule.vintasoftware.com"

  # Lock the CORS origins to the real frontend before going live.
  cors_allowed_origins = [
    "https://schedule.vintasoftware.com",
  ]
}
