# Staging environment-level inputs, read by root.hcl and by this environment's
# terragrunt.hcl.
locals {
  environment = "staging"

  # One workspace per environment: storage and the app platform share a single
  # Terraform state, so a single Scalr run applies both. This is the workspace
  # that used to hold the storage-only stack -- its state was renamed under
  # `module.storage` rather than recreated. See infrastructure/README.md.
  scalr_workspace = "VintaScheduleStaging"

  aws_region = "us-east-1"

  # Route 53 hosted zone the custom domains live under.
  route53_zone_name = "vintasoftware.com"

  # Role in the DNS account that Terraform assumes to write Route 53 records.
  dns_role_arn = "arn:aws:iam::310361226925:role/vinta-schedule-dns-deployer"

  # Repository whose `main` workflow runs may assume the ECS deploy role.
  github_repository = "vintasoftware/vinta-schedule-api"
}
