# Staging environment-level inputs, merged into every stack under this dir.
locals {
  environment = "staging"

  # One Scalr workspace per stack folder -- state does not cross workspaces, so
  # `storage` and `app` cannot share one. root.hcl looks the folder name up here.
  scalr_workspaces = {
    storage = "VintaScheduleStaging"
    app     = "VintaScheduleStagingApp"
  }

  aws_region = "us-east-1"

  # Route 53 hosted zone the custom domains live under.
  route53_zone_name = "vintasoftware.com"

  # Role in the DNS account that Terraform assumes to write Route 53 records.
  dns_role_arn = "arn:aws:iam::310361226925:role/vinta-schedule-dns-deployer"

  # Repository whose `main` workflow runs may assume the ECS deploy role.
  github_repository = "vintasoftware/vinta-schedule-api"
}
