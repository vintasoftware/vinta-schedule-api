variable "project_name" {
  description = "Project slug used to derive resource names."
  type        = string
}

variable "environment" {
  description = "Environment slug (e.g. production, staging)."
  type        = string
}

variable "media_bucket_name" {
  description = "Explicit media bucket name. Defaults to <project>-<env>-media when empty."
  type        = string
  default     = ""
  nullable    = false
}

variable "static_bucket_name" {
  description = "Explicit static bucket name. Defaults to <project>-<env>-static when empty."
  type        = string
  default     = ""
  nullable    = false
}

variable "price_class" {
  description = "CloudFront price class."
  type        = string
  default     = "PriceClass_100"
  nullable    = false
}

variable "static_domain" {
  description = "Custom domain for the static CloudFront distribution (e.g. static.schedule.vintasoftware.com)."
  type        = string
}

variable "media_domain" {
  description = "Custom domain for the media CloudFront distribution (e.g. media.schedule.vintasoftware.com)."
  type        = string
}

variable "dns_provider" {
  description = "Where the custom domains' DNS lives: \"route53\" (through the aws.dns provider) or \"cloudflare\"."
  type        = string
  default     = "route53"
  nullable    = false

  validation {
    condition     = contains(["route53", "cloudflare"], var.dns_provider)
    error_message = "dns_provider must be \"route53\" or \"cloudflare\"."
  }
}

variable "dns_zone_name" {
  description = "Zone the custom domains live under (e.g. vintasoftware.com), in Route 53 or Cloudflare per dns_provider. No trailing dot."
  type        = string
}

variable "cors_allowed_origins" {
  description = "Origins allowed to upload directly to the media bucket (django-s3direct)."
  type        = list(string)
  default     = ["*"]
  nullable    = false
}
