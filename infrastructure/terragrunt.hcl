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
  project = "vinta-schedule"

  scalr_hostname    = get_env("SCALR_HOSTNAME", "vinta.scalr.io")
  scalr_environment = get_env("SCALR_ENVIRONMENT", "VintaSchedule")

  # Workspace name is taken from the including environment's env.hcl, so it can
  # match whatever you named the workspace in Scalr.
  env = read_terragrunt_config(find_in_parent_folders("env.hcl"))
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
      name = local.env.locals.scalr_workspace
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

    # Route 53 lives in a different AWS account; this aliased provider assumes a
    # role there so Terraform can write the ACM-validation and alias records.
    provider "aws" {
      alias  = "dns"
      region = var.aws_region

      assume_role {
        role_arn = var.dns_role_arn
      }
    }

    variable "aws_region" {
      type    = string
      default = "us-east-1"
    }

    variable "dns_role_arn" {
      type = string
    }
  EOF
}
