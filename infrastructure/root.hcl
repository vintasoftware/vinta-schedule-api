# Root Terragrunt config.
#
# Remote state + runs are backed by Scalr (Terraform Cloud/Enterprise-compatible
# `remote` backend). Set these before running:
#   SCALR_HOSTNAME      e.g. example.scalr.io
#   SCALR_ENVIRONMENT   the Scalr environment (maps to TFC "organization")
#   SCALR_TOKEN         API token (via `terraform login <hostname>` or env)
# AWS credentials for the run come from the Scalr workspace (provider config /
# shell variables), never from this repo.

# Terragrunt defaults to the `tofu` binary when one is on PATH, and OpenTofu
# 1.12 refuses to touch a workspace pinned to 1.5.7 ("version mismatch ... you
# can force ... with -ignore-remote-version"). Use `terraform`, which tfenv
# resolves to the version in .terraform-version. The constraint turns a
# too-new binary into a clear error instead of the Scalr backend's "Please
# downgrade Terraform to <= 1.5.99" at init time.
terraform_binary             = "terraform"
terraform_version_constraint = ">= 1.5, <= 1.5.99"

locals {
  project = "vinta-schedule"

  scalr_hostname    = get_env("SCALR_HOSTNAME", "vinta.scalr.io")
  scalr_environment = get_env("SCALR_ENVIRONMENT", "VintaSchedule")

  # One stack per environment, so `env.hcl` sits next to the environment's own
  # terragrunt.hcl instead of a folder above it. `get_terragrunt_dir()` in an
  # included config resolves to the *child* directory, which is what makes this
  # read the right environment.
  env = read_terragrunt_config("${get_terragrunt_dir()}/env.hcl")

  scalr_workspace = local.env.locals.scalr_workspace
}

# The `remote` backend needs `workspaces` as a BLOCK, not an argument —
# terragrunt's remote_state renders it as `workspaces = {}`, which Terraform
# rejects. Generate the backend directly so the block syntax is correct.
generate "backend" {
  path      = "backend.tf"
  if_exists = "overwrite"
  contents  = <<-EOT
    terraform {
      backend "remote" {
        hostname     = "${local.scalr_hostname}"
        organization = "${local.scalr_environment}"

        workspaces {
          name = "${local.scalr_workspace}"
        }
      }
    }
  EOT
}

# Inject the AWS providers into the environment module so each environment's
# terragrunt.hcl doesn't repeat them. `aws_region` and `dns_role_arn` are
# declared by modules/environment itself.
generate "provider" {
  path      = "provider.tf"
  if_exists = "overwrite"
  contents  = <<-EOT
    provider "aws" {
      region = var.aws_region

      default_tags {
        tags = {
          Project     = "${local.project}"
          Environment = "${local.env.locals.environment}"
          ManagedBy   = "terragrunt"
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
  EOT
}
