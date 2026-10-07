########################################
# API Gateway ingress (ingress_mode = "api_gateway")
#
# An HTTP API in place of the ALB, for an environment whose traffic is too small
# to justify the ALB's fixed charge: API Gateway bills per request, and the VPC
# link and Cloud Map namespace cost next to nothing.
#
#   client -> api.<env> (custom domain, TLS) -> HTTP API -> VPC link
#          -> Cloud Map (SRV records ECS keeps current) -> web task :8000
#
# Limits that come with it, none of which the ALB has:
#   - 30-second integration timeout, not raisable.
#   - 10 MB request body.
#   - No AWS WAF (WAF attaches to REST APIs, not HTTP APIs).
#   - No connection draining: a task leaving Cloud Map mid-request drops it,
#     so a deploy can surface a few 5xx responses.
#   - Plain HTTP on port 80 is not answered at all, rather than redirected.
#
# Headers. API Gateway rewrites X-Forwarded-For/-Proto/-Host into a single
# RFC 7239 `Forwarded` header and refuses mappings onto any of them, so Django's
# usual proxy headers never arrive. The integration therefore writes two of its
# own, which the containers are told to read (CLIENT_IP_HEADER and
# PROXY_SSL_HEADER in ecs.tf). `overwrite` replaces anything a client sent under
# the same name, and the tasks accept connections from the VPC link only, so
# neither can be forged.
########################################

locals {
  use_api_gateway = var.ingress_mode == "api_gateway"
}

resource "aws_cloudwatch_log_group" "api_gateway" {
  count = local.use_api_gateway ? 1 : 0

  name              = "/aws/apigateway/${local.name_prefix}"
  retention_in_days = var.log_retention_days
}

########################################
# Service discovery
#
# API Gateway finds the tasks through Cloud Map, and needs a port as well as
# an address -- which ECS only registers for SRV records. A private DNS
# namespace is the type ECS can register into; it brings a Route 53 private
# hosted zone ($0.50/month) with it.
########################################

resource "aws_service_discovery_private_dns_namespace" "this" {
  count = local.use_api_gateway ? 1 : 0

  name        = "${local.name_prefix}.internal"
  description = "Web tasks, for the API Gateway VPC link."
  vpc         = aws_vpc.this.id
}

resource "aws_service_discovery_service" "web" {
  count = local.use_api_gateway ? 1 : 0

  name = "web"

  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.this[0].id
    routing_policy = "MULTIVALUE"

    dns_records {
      type = "SRV"
      ttl  = 10
    }
  }

  # ECS reports health from the container health check in ecs.tf, and Cloud Map
  # stops returning a task as soon as that check fails.
  # (`failure_threshold` is deprecated and fixed at 1, so it is left unset.)
  health_check_custom_config {}
}

########################################
# HTTP API
########################################

resource "aws_apigatewayv2_vpc_link" "this" {
  count = local.use_api_gateway ? 1 : 0

  name               = local.name_prefix
  subnet_ids         = aws_subnet.private[*].id
  security_group_ids = [aws_security_group.vpc_link[0].id]
}

resource "aws_apigatewayv2_api" "this" {
  count = local.use_api_gateway ? 1 : 0

  name          = local.name_prefix
  protocol_type = "HTTP"

  # Only the custom domain answers. The generated execute-api hostname would be
  # a second way in that the Host rewrite below does not account for.
  disable_execute_api_endpoint = true
}

resource "aws_apigatewayv2_integration" "web" {
  count = local.use_api_gateway ? 1 : 0

  api_id             = aws_apigatewayv2_api.this[0].id
  integration_type   = "HTTP_PROXY"
  integration_method = "ANY"
  integration_uri    = aws_service_discovery_service.web[0].arn
  connection_type    = "VPC_LINK"
  connection_id      = aws_apigatewayv2_vpc_link.this[0].id

  payload_format_version = "1.0"

  # The ceiling. Gunicorn's own 30-second worker timeout is the same, so a slow
  # request is still killed by the application, which logs it.
  timeout_milliseconds = 30000

  request_parameters = {
    # ALLOWED_HOSTS and every absolute URL Django builds read the Host header,
    # which would otherwise carry the task's address. API Gateway accepts only a
    # constant here, not a $context variable. The custom domain is the API's
    # only way in (the execute-api endpoint is disabled), so the constant is
    # always right.
    "overwrite:header.Host" = var.api_domain
    # Read through CLIENT_IP_HEADER by rate limiting and audit logging.
    "overwrite:header.X-Client-IP" = "$context.identity.sourceIp"
    # Read through PROXY_SSL_HEADER; without it every request looks like plain
    # HTTP and SECURE_SSL_REDIRECT answers each one with a redirect to itself.
    "overwrite:header.X-Client-Proto" = "https"
  }
}

resource "aws_apigatewayv2_route" "default" {
  count = local.use_api_gateway ? 1 : 0

  api_id    = aws_apigatewayv2_api.this[0].id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.web[0].id}"
}

# `$default` is the one stage name API Gateway does not prepend to the path it
# sends a private integration, so Django's URLs need no rewriting.
resource "aws_apigatewayv2_stage" "default" {
  count = local.use_api_gateway ? 1 : 0

  api_id      = aws_apigatewayv2_api.this[0].id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_rate_limit  = var.api_gateway_throttling_rate_limit
    throttling_burst_limit = var.api_gateway_throttling_burst_limit
  }

  # The ALB had no access log; gunicorn's covered it. This one is kept because
  # it is the only record of requests API Gateway answers itself (429s, 503s
  # when no task is registered, 504s past the timeout) -- gunicorn never sees
  # those. The client IP is left out on purpose: it is an identifier, and this
  # would be a new place storing it next to request paths. requestId is enough
  # to correlate with the application's own logs.
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gateway[0].arn
    format = jsonencode({
      requestId          = "$context.requestId"
      requestTime        = "$context.requestTime"
      httpMethod         = "$context.httpMethod"
      path               = "$context.path"
      status             = "$context.status"
      responseLength     = "$context.responseLength"
      integrationStatus  = "$context.integrationStatus"
      integrationLatency = "$context.integrationLatency"
      errorMessage       = "$context.error.message"
    })
  }
}

########################################
# Custom domain
#
# Reuses the ACM certificate alb.tf issues -- a regional API Gateway domain
# takes a certificate from the same region, which that one is.
########################################

resource "aws_apigatewayv2_domain_name" "api" {
  count = local.use_api_gateway ? 1 : 0

  domain_name = var.api_domain

  domain_name_configuration {
    certificate_arn = aws_acm_certificate_validation.api.certificate_arn
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }
}

resource "aws_apigatewayv2_api_mapping" "api" {
  count = local.use_api_gateway ? 1 : 0

  api_id      = aws_apigatewayv2_api.this[0].id
  domain_name = aws_apigatewayv2_domain_name.api[0].id
  stage       = aws_apigatewayv2_stage.default[0].id
}
