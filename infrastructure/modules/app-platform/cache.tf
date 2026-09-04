########################################
# ElastiCache
#
# Backs three things at once: the Celery result backend, redbeat's schedule and
# lock, and django-defender's login throttling. It is NOT the Celery broker --
# that is SQS (see queue.tf).
########################################

resource "aws_elasticache_subnet_group" "this" {
  name       = local.name_prefix
  subnet_ids = aws_subnet.private[*].id
}

resource "random_password" "cache_auth_token" {
  length = 64
  # ElastiCache rejects several punctuation characters in an AUTH token, and the
  # token is carried in the REDIS_URL userinfo where anything special would need
  # percent-encoding. Alphanumeric avoids both problems.
  special = false
}

resource "aws_elasticache_replication_group" "this" {
  replication_group_id = local.name_prefix
  description          = "${local.name_prefix} cache, result backend and redbeat store."

  engine         = var.cache_engine
  engine_version = var.cache_engine_version
  node_type      = var.cache_node_type
  port           = 6379

  num_cache_clusters         = var.cache_node_count
  automatic_failover_enabled = var.cache_node_count > 1
  multi_az_enabled           = var.cache_node_count > 1

  subnet_group_name  = aws_elasticache_subnet_group.this.name
  security_group_ids = [aws_security_group.cache.id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = random_password.cache_auth_token.result

  # Nothing here is a source of truth -- results, schedules and throttle counters
  # all rebuild themselves -- so snapshots would only cost money.
  snapshot_retention_limit = 0

  maintenance_window         = "mon:07:00-mon:08:00"
  auto_minor_version_upgrade = true
  apply_immediately          = false

  lifecycle {
    # Same reason as the RDS instance: a major-only version is echoed back in full.
    ignore_changes = [engine_version]
  }

  tags = {
    Name = local.name_prefix
  }
}

locals {
  # `ssl_cert_reqs` is not optional decoration: celery refuses a `rediss://`
  # result-backend URL that does not carry it. `required` validates against the
  # system CA bundle, which already trusts the Amazon Trust roots ElastiCache
  # presents.
  redis_url = format(
    "rediss://:%s@%s:%s/0?ssl_cert_reqs=required",
    random_password.cache_auth_token.result,
    aws_elasticache_replication_group.this.primary_endpoint_address,
    aws_elasticache_replication_group.this.port,
  )
}
