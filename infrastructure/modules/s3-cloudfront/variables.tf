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
}

variable "static_bucket_name" {
  description = "Explicit static bucket name. Defaults to <project>-<env>-static when empty."
  type        = string
  default     = ""
}

variable "price_class" {
  description = "CloudFront price class."
  type        = string
  default     = "PriceClass_100"
}

variable "static_domain" {
  description = "Custom domain for the static CloudFront distribution (e.g. static.schedule.vintasoftware.com)."
  type        = string
}

variable "media_domain" {
  description = "Custom domain for the media CloudFront distribution (e.g. media.schedule.vintasoftware.com)."
  type        = string
}

variable "route53_zone_name" {
  description = "Route 53 hosted zone the custom domains live under (e.g. vintasoftware.com). No trailing dot."
  type        = string
}

variable "cors_allowed_origins" {
  description = "Origins allowed to upload directly to the media bucket (django-s3direct)."
  type        = list(string)
  default     = ["*"]
}
