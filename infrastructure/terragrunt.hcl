# Root Terragrunt config.
#
# Remote state + runs are backed by Scalr (Terraform Cloud/Enterprise-compatible
# `remote` backend). Set these before running:
#   SCALR_HOSTNAME      e.g. example.scalr.io
#   SCALR_ENVIRONMENT   the Scalr environment (maps to TFC "organization")
#   SCALR_TOKEN         API token (via `terraform login <hostname>` or env)
# AWS credentials for the run come from the Scalr workspace (provider config /
# shell variables), never from this repo.

locals {
  project          = "vinta-schedule"
  scalr_hostname   = get_env("SCALR_HOSTNAME", "example.scalr.io")
  scalr_environment = get_env("SCALR_ENVIRONMENT", "vinta-schedule")
}

remote_state {
  backend = "remote"

  generate = {
    path      = "backend.tf"
    if_exists = "overwrite"
  }

  config = {
    hostname     = local.scalr_hostname
    organization = local.scalr_environment

    workspaces = {
      # One Scalr workspace per stack, e.g. vinta-schedule-environments-production-storage.
      name = "${local.project}-${replace(path_relative_to_include(), "/", "-")}"
    }
  }
}

# Inject the AWS provider into every stack so child terragrunt.hcl files don't repeat it.
generate "provider" {
  path      = "provider.tf"
  if_exists = "overwrite"
  contents  = <<-EOF
    provider "aws" {
      region = var.aws_region

      default_tags {
        tags = {
          Project   = "${local.project}"
          ManagedBy = "terragrunt"
        }
      }
    }

    variable "aws_region" {
      type    = string
      default = "us-east-1"
    }
  EOF
}
