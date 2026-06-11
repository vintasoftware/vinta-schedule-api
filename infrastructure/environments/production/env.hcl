# Production environment-level inputs, merged into every stack under this dir.
locals {
  environment = "production"

  # Scalr workspace backing this environment's state.
  scalr_workspace = "VintaScheduleProduction"

  aws_region = "us-east-1"

  # Route 53 hosted zone the custom domains live under.
  route53_zone_name = "vintasoftware.com"

  # Role in the DNS account that Terraform assumes to write Route 53 records.
  dns_role_arn = "arn:aws:iam::SET_ME_DNS_ACCOUNT_ID:role/vinta-schedule-dns-deployer"
}
