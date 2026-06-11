# Staging environment-level inputs, merged into every stack under this dir.
locals {
  environment = "staging"

  # Scalr workspace backing this environment's state.
  scalr_workspace = "VintaScheduleStaging"

  aws_region = "us-east-1"

  # Route 53 hosted zone the custom domains live under.
  route53_zone_name = "vintasoftware.com"

  # Role in the DNS account that Terraform assumes to write Route 53 records.
  dns_role_arn = "arn:aws:iam::310361226925:role/vinta-schedule-dns-deployer"
}
