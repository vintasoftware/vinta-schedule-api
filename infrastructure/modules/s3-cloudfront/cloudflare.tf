########################################
# Cloudflare DNS (dns_provider = "cloudflare")
#
# The same records main.tf writes to Route 53, minus the CAA. Cloudflare has no
# alias records, so each hostname is a CNAME to its distribution, and a CNAME
# may not share its name with any other record -- the per-host CAA that
# authorizes ACM on Route 53 cannot exist here. See "Cloudflare DNS" in
# infrastructure/README.md for what that means for certificate renewal.
#
# Every record is DNS-only: CloudFront terminates TLS with the ACM certificate,
# and proxying through Cloudflare would put a second CDN in front of it.
########################################

data "cloudflare_zone" "this" {
  count = local.use_cloudflare ? 1 : 0

  filter = {
    name = var.dns_zone_name
  }
}

resource "cloudflare_dns_record" "cert_validation" {
  for_each = local.use_cloudflare ? local.cert_validation_records : {}

  zone_id = data.cloudflare_zone.this[0].zone_id
  # ACM hands both out as FQDNs with a trailing dot, which Cloudflare drops on
  # save -- left in, every plan would show a change.
  name    = trimsuffix(each.value.name, ".")
  type    = each.value.type
  content = trimsuffix(each.value.record, ".")
  ttl     = 60
  proxied = false
}

resource "cloudflare_dns_record" "static" {
  count = local.use_cloudflare ? 1 : 0

  zone_id = data.cloudflare_zone.this[0].zone_id
  name    = var.static_domain
  type    = "CNAME"
  content = aws_cloudfront_distribution.static.domain_name
  ttl     = 300
  proxied = false
}

resource "cloudflare_dns_record" "media" {
  count = local.use_cloudflare ? 1 : 0

  zone_id = data.cloudflare_zone.this[0].zone_id
  name    = var.media_domain
  type    = "CNAME"
  content = aws_cloudfront_distribution.media.domain_name
  ttl     = 300
  proxied = false
}
