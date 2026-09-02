########################################
# TLS certificate
#
# Issued by ACM in the deploy account, validated by DNS records written into the
# hosted zone in the *other* account through the `aws.dns` provider that
# root.hcl generates. ACM renews it automatically for as long as those validation
# records stay in place -- deleting them silently breaks renewal a year later.
########################################

data "aws_route53_zone" "this" {
  provider     = aws.dns
  name         = var.route53_zone_name
  private_zone = false
}

resource "aws_acm_certificate" "api" {
  domain_name       = var.api_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.api.domain_validation_options : dvo.domain_name => {
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

resource "aws_acm_certificate_validation" "api" {
  certificate_arn         = aws_acm_certificate.api.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

########################################
# Load balancer
#
# The only thing in this stack with a public address. Everything it forwards to
# lives in the private subnets.
########################################

resource "aws_lb" "this" {
  name               = local.name_prefix
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  # Longer than gunicorn's own 30s worker timeout, so a slow request is killed by
  # the application (which logs it) rather than cut off by the load balancer.
  idle_timeout = 60

  # Reject requests whose headers the ALB and gunicorn would disagree about --
  # the shape of request smuggling that gets past a permissive proxy.
  drop_invalid_header_fields = true

  enable_deletion_protection = var.alb_deletion_protection

  tags = {
    Name = local.name_prefix
  }
}

resource "aws_lb_target_group" "web" {
  name        = "${local.name_prefix}-web"
  port        = var.container_port
  protocol    = "HTTP"
  vpc_id      = aws_vpc.this.id
  target_type = "ip"

  # Fargate replaces a task with a new IP on every deploy, so the window between
  # "ALB stops sending new requests" and "task exits" needs to cover only the
  # in-flight ones.
  deregistration_delay = 30

  health_check {
    path                = var.health_check_path
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = {
    Name = "${local.name_prefix}-web"
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  # Django's own SECURE_SSL_REDIRECT would do this too, but only after the request
  # has travelled unencrypted all the way to a task.
  default_action {
    type = "redirect"

    redirect {
      protocol    = "HTTPS"
      port        = "443"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.api.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
}

########################################
# api.<env>.vintasoftware.com -> ALB
########################################

resource "aws_route53_record" "api" {
  provider = aws.dns
  zone_id  = data.aws_route53_zone.this.zone_id
  name     = var.api_domain
  type     = "A"

  alias {
    name                   = aws_lb.this.dns_name
    zone_id                = aws_lb.this.zone_id
    evaluate_target_health = false
  }
}
