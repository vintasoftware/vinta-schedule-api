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

variable "cors_allowed_origins" {
  description = "Origins allowed to upload directly to the media bucket (django-s3direct)."
  type        = list(string)
  default     = ["*"]
}
