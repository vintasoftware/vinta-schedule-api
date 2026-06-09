# Map each output to the Render env var it feeds (aws-storage group).

output "media_bucket_name" {
  description = "AWS_MEDIA_BUCKET_NAME"
  value       = aws_s3_bucket.media.bucket
}

output "static_bucket_name" {
  description = "AWS_STATIC_BUCKET_NAME"
  value       = aws_s3_bucket.static.bucket
}

output "media_cloudfront_domain" {
  description = "AWS_MEDIA_S3_CUSTOM_DOMAIN"
  value       = aws_cloudfront_distribution.media.domain_name
}

output "static_cloudfront_domain" {
  description = "AWS_STATIC_S3_CUSTOM_DOMAIN"
  value       = aws_cloudfront_distribution.static.domain_name
}

output "media_s3_endpoint_url" {
  description = "AWS_MEDIA_S3_ENDPOINT_URL"
  value       = "https://s3.${aws_s3_bucket.media.region}.amazonaws.com"
}

output "cloudfront_key_id" {
  description = "AWS_CLOUDFRONT_KEY_ID"
  value       = aws_cloudfront_public_key.media.id
}

output "cloudfront_private_key" {
  description = "AWS_CLOUDFRONT_KEY (PEM private key — paste into Render as-is)"
  value       = tls_private_key.cloudfront.private_key_pem
  sensitive   = true
}

output "aws_access_key_id" {
  description = "AWS_ACCESS_KEY_ID"
  value       = aws_iam_access_key.app.id
}

output "aws_secret_access_key" {
  description = "AWS_SECRET_ACCESS_KEY"
  value       = aws_iam_access_key.app.secret
  sensitive   = true
}
