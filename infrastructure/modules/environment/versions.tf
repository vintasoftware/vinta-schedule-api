terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# `tls` (CloudFront signing key) and `random` (database password, cache AUTH
# token) are required by the child modules, not here -- Terraform resolves them
# transitively, but .terraform.lock.hcl in the calling environment folder still
# has to carry all three.
