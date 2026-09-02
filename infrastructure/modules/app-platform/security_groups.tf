########################################
# Security groups
#
# The chain is deliberately one-directional: internet -> ALB -> ECS tasks ->
# RDS / ElastiCache. Each hop only accepts traffic from the security group one
# step above it, so nothing in the data tier is reachable even from another
# resource inside the VPC.
########################################

resource "aws_security_group" "alb" {
  name        = "${local.name_prefix}-alb"
  description = "Public entry point for the API load balancer."
  vpc_id      = aws_vpc.this.id

  tags = {
    Name = "${local.name_prefix}-alb"
  }
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP, redirected to HTTPS by the listener."
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from the internet."
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_tasks" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Forward to the web tasks."
  referenced_security_group_id = aws_security_group.ecs_tasks.id
  from_port                    = var.container_port
  to_port                      = var.container_port
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "ecs_tasks" {
  name        = "${local.name_prefix}-ecs-tasks"
  description = "Web, worker, beat and release tasks."
  vpc_id      = aws_vpc.this.id

  tags = {
    Name = "${local.name_prefix}-ecs-tasks"
  }
}

resource "aws_vpc_security_group_ingress_rule" "tasks_from_alb" {
  security_group_id            = aws_security_group.ecs_tasks.id
  description                  = "Only the load balancer may reach gunicorn."
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = var.container_port
  to_port                      = var.container_port
  ip_protocol                  = "tcp"
}

# Tasks talk out to ECR, CloudWatch Logs, Secrets Manager, SQS and the third-party
# calendar / payment / SMS APIs -- all reached over the NAT gateway, none of them a
# fixed address worth enumerating.
resource "aws_vpc_security_group_egress_rule" "tasks_all" {
  security_group_id = aws_security_group.ecs_tasks.id
  description       = "Outbound to AWS APIs and third-party integrations."
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_security_group" "database" {
  name        = "${local.name_prefix}-database"
  description = "Postgres, reachable only from ECS tasks."
  vpc_id      = aws_vpc.this.id

  tags = {
    Name = "${local.name_prefix}-database"
  }
}

resource "aws_vpc_security_group_ingress_rule" "database_from_tasks" {
  security_group_id            = aws_security_group.database.id
  description                  = "Postgres from the ECS tasks."
  referenced_security_group_id = aws_security_group.ecs_tasks.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "cache" {
  name        = "${local.name_prefix}-cache"
  description = "ElastiCache, reachable only from ECS tasks."
  vpc_id      = aws_vpc.this.id

  tags = {
    Name = "${local.name_prefix}-cache"
  }
}

resource "aws_vpc_security_group_ingress_rule" "cache_from_tasks" {
  security_group_id            = aws_security_group.cache.id
  description                  = "Redis protocol from the ECS tasks."
  referenced_security_group_id = aws_security_group.ecs_tasks.id
  from_port                    = 6379
  to_port                      = 6379
  ip_protocol                  = "tcp"
}
