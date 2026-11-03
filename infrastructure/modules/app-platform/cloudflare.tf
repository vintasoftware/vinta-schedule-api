########################################
# Cloudflare DNS (dns_provider = "cloudflare")
#
# The same records alb.tf writes to Route 53, minus the CAA. Cloudflare has no
# alias records, so the api name is a CNAME to the ALB or the API Gateway
# domain, and a CNAME may not share its name with any other record -- the
# per-host CAA that authorizes ACM on Route 53 cannot exist here. See
# "Cloudflare DNS" in infrastructure/README.md for what that means for
# certificate renewal.
#
# DNS-only: the ALB or API Gateway terminates TLS with the ACM certificate.
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

resource "cloudflare_dns_record" "api" {
  count = local.use_cloudflare ? 1 : 0

  zone_id = data.cloudflare_zone.this[0].zone_id
  name    = var.api_domain
  type    = "CNAME"
  content = (
    local.use_alb
    ? aws_lb.this[0].dns_name
    : aws_apigatewayv2_domain_name.api[0].domain_name_configuration[0].target_domain_name
  )
  ttl     = 300
  proxied = false

  # Behind API Gateway, the domain answers 404 until the mapping to the stage
  # exists, so DNS waits for it. Empty in ALB mode.
  depends_on = [aws_apigatewayv2_api_mapping.api]
}
