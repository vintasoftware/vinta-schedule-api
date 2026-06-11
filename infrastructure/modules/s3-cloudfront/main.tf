locals {
  media_bucket  = var.media_bucket_name != "" ? var.media_bucket_name : "${var.project_name}-${var.environment}-media"
  static_bucket = var.static_bucket_name != "" ? var.static_bucket_name : "${var.project_name}-${var.environment}-static"
  name_prefix   = "${var.project_name}-${var.environment}"
}

# CloudFront-managed cache policy (no need to author our own).
data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

########################################
# Custom domains: ACM cert (DNS-validated) + Route 53
#
# CloudFront only accepts ACM certs from us-east-1 — the provider must be
# us-east-1 (env.hcl sets aws_region = "us-east-1").
########################################

data "aws_route53_zone" "this" {
  provider     = aws.dns
  name         = var.route53_zone_name
  private_zone = false
}

# One cert covering both the static and media hostnames.
resource "aws_acm_certificate" "cdn" {
  domain_name               = var.static_domain
  subject_alternative_names = [var.media_domain]
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.cdn.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      type   = dvo.resource_record_type
      record = dvo.resource_record_value
    }
  }

  provider        = aws.dns
  zone_id         = data.aws_route53_zone.this.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "cdn" {
  certificate_arn         = aws_acm_certificate.cdn.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

########################################
# S3 buckets
########################################

resource "aws_s3_bucket" "media" {
  bucket = local.media_bucket
}

resource "aws_s3_bucket" "static" {
  bucket = local.static_bucket
}

resource "aws_s3_bucket_public_access_block" "media" {
  bucket                  = aws_s3_bucket.media.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_public_access_block" "static" {
  bucket                  = aws_s3_bucket.static.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "media" {
  bucket = aws_s3_bucket.media.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_ownership_controls" "static" {
  bucket = aws_s3_bucket.static.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Direct browser uploads to media (django-s3direct). Mirrors minio-cors.json.
resource "aws_s3_bucket_cors_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["GET", "PUT", "POST", "HEAD"]
    allowed_origins = var.cors_allowed_origins
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}

########################################
# CloudFront Origin Access Control
########################################

resource "aws_cloudfront_origin_access_control" "media" {
  name                              = "${local.name_prefix}-media-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_origin_access_control" "static" {
  name                              = "${local.name_prefix}-static-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

########################################
# Signed-URL key pair (media only)
#
# Django (django-storages) signs media URLs with the PEM private key, keyed by
# the CloudFront public key id. The distribution enforces signing via the key
# group on its cache behavior.
########################################

resource "tls_private_key" "cloudfront" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "aws_cloudfront_public_key" "media" {
  name        = "${local.name_prefix}-media-key"
  encoded_key = tls_private_key.cloudfront.public_key_pem
  comment     = "Signed-URL key for ${local.name_prefix} media"
}

resource "aws_cloudfront_key_group" "media" {
  name  = "${local.name_prefix}-media-key-group"
  items = [aws_cloudfront_public_key.media.id]
}

########################################
# CloudFront distributions
########################################

# Media: private origin, every request must carry a valid signature.
resource "aws_cloudfront_distribution" "media" {
  enabled         = true
  comment         = "${local.name_prefix} media (signed)"
  price_class     = var.price_class
  is_ipv6_enabled = true
  aliases         = [var.media_domain]

  origin {
    domain_name              = aws_s3_bucket.media.bucket_regional_domain_name
    origin_id                = "media-s3"
    origin_access_control_id = aws_cloudfront_origin_access_control.media.id
  }

  default_cache_behavior {
    target_origin_id       = "media-s3"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = data.aws_cloudfront_cache_policy.optimized.id
    trusted_key_groups     = [aws_cloudfront_key_group.media.id]
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.cdn.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

# Static: public read through CloudFront, no signing.
resource "aws_cloudfront_distribution" "static" {
  enabled         = true
  comment         = "${local.name_prefix} static"
  price_class     = var.price_class
  is_ipv6_enabled = true
  aliases         = [var.static_domain]

  origin {
    domain_name              = aws_s3_bucket.static.bucket_regional_domain_name
    origin_id                = "static-s3"
    origin_access_control_id = aws_cloudfront_origin_access_control.static.id
  }

  default_cache_behavior {
    target_origin_id       = "static-s3"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = data.aws_cloudfront_cache_policy.optimized.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.cdn.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

# DNS: point each custom domain at its CloudFront distribution (A + AAAA aliases).
resource "aws_route53_record" "static" {
  provider = aws.dns
  zone_id  = data.aws_route53_zone.this.zone_id
  name     = var.static_domain
  type     = "A"
  alias {
    name                   = aws_cloudfront_distribution.static.domain_name
    zone_id                = aws_cloudfront_distribution.static.hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "static_aaaa" {
  provider = aws.dns
  zone_id  = data.aws_route53_zone.this.zone_id
  name     = var.static_domain
  type     = "AAAA"
  alias {
    name                   = aws_cloudfront_distribution.static.domain_name
    zone_id                = aws_cloudfront_distribution.static.hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "media" {
  provider = aws.dns
  zone_id  = data.aws_route53_zone.this.zone_id
  name     = var.media_domain
  type     = "A"
  alias {
    name                   = aws_cloudfront_distribution.media.domain_name
    zone_id                = aws_cloudfront_distribution.media.hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "media_aaaa" {
  provider = aws.dns
  zone_id  = data.aws_route53_zone.this.zone_id
  name     = var.media_domain
  type     = "AAAA"
  alias {
    name                   = aws_cloudfront_distribution.media.domain_name
    zone_id                = aws_cloudfront_distribution.media.hosted_zone_id
    evaluate_target_health = false
  }
}

########################################
# Bucket policies — only the matching distribution may read
########################################

data "aws_iam_policy_document" "media_bucket" {
  statement {
    sid       = "AllowCloudFrontRead"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.media.arn}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.media.arn]
    }
  }
}

data "aws_iam_policy_document" "static_bucket" {
  statement {
    sid       = "AllowCloudFrontRead"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.static.arn}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.static.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "media" {
  bucket = aws_s3_bucket.media.id
  policy = data.aws_iam_policy_document.media_bucket.json
}

resource "aws_s3_bucket_policy" "static" {
  bucket = aws_s3_bucket.static.id
  policy = data.aws_iam_policy_document.static_bucket.json
}

########################################
# IAM user the app uses to upload objects
#
# Render is not on AWS, so we hand the app long-lived access keys rather than an
# instance role. Scope is limited to these two buckets.
########################################

resource "aws_iam_user" "app" {
  name = "${local.name_prefix}-storage"
}

data "aws_iam_policy_document" "app" {
  statement {
    sid = "ObjectAccess"
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:DeleteObject",
    ]
    resources = [
      "${aws_s3_bucket.media.arn}/*",
      "${aws_s3_bucket.static.arn}/*",
    ]
  }

  statement {
    sid       = "BucketList"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.media.arn, aws_s3_bucket.static.arn]
  }
}

resource "aws_iam_user_policy" "app" {
  name   = "s3-access"
  user   = aws_iam_user.app.name
  policy = data.aws_iam_policy_document.app.json
}

resource "aws_iam_access_key" "app" {
  user = aws_iam_user.app.name
}
