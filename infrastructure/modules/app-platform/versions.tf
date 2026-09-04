terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"

      # This module is composed by modules/environment, which owns the provider
      # blocks. `aws.dns` (the role in the Route 53 account) has to be declared
      # here so the caller is required to pass it in.
      configuration_aliases = [aws.dns]
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}
