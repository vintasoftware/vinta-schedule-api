########################################
# RDS Postgres
#
# Private-subnet only: `publicly_accessible = false` plus a subnet group made of
# the private subnets means the instance has no route from the internet at all,
# not merely a closed security group. Reaching it from a laptop needs a bastion
# or `aws ecs execute-command` into a running task.
########################################

resource "aws_db_subnet_group" "this" {
  name       = local.name_prefix
  subnet_ids = aws_subnet.private[*].id

  tags = {
    Name = local.name_prefix
  }
}

resource "random_password" "db" {
  length = 40
  # RDS rejects `/`, `@`, `"` and space in a master password, and the value ends
  # up inside a DATABASE_URL, where anything needing percent-encoding is a
  # footgun. Alphanumeric sidesteps both at 40 characters of entropy.
  special = false
}

resource "aws_db_parameter_group" "this" {
  name        = local.name_prefix
  family      = "postgres${split(".", var.db_engine_version)[0]}"
  description = "${local.name_prefix} Postgres parameters."

  # Refuse unencrypted connections. Django reaches the database over the private
  # subnets, but TLS costs nothing here and makes the guarantee explicit.
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  # Log statements that would show up as a slow endpoint before they show up as
  # an outage. 1s is loose enough not to spam the log at this instance size.
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "this" {
  identifier = local.name_prefix

  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db.result
  port     = 5432

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.database.id]
  publicly_accessible    = false
  multi_az               = var.db_multi_az

  parameter_group_name = aws_db_parameter_group.this.name

  backup_retention_period = var.db_backup_retention_days
  backup_window           = "05:00-06:00"
  maintenance_window      = "Mon:06:00-Mon:07:00"
  copy_tags_to_snapshot   = true

  auto_minor_version_upgrade = true
  apply_immediately          = false

  # One flag decides both: an environment worth protecting from `terraform destroy`
  # is also one whose data is worth a final snapshot on the way out.
  deletion_protection       = var.db_deletion_protection
  skip_final_snapshot       = !var.db_deletion_protection
  final_snapshot_identifier = var.db_deletion_protection ? "${local.name_prefix}-final" : null

  # Postgres logs are the only ones worth the CloudWatch ingest cost here; the
  # upgrade log is noise.
  enabled_cloudwatch_logs_exports = ["postgresql"]

  lifecycle {
    # `engine_version` is major-only, so RDS reports back a full `17.x` and every
    # subsequent plan would want to "change" it back.
    ignore_changes = [engine_version]
  }

  # Nothing links these two, but order decides whether the apply works: RDS creates
  # the log group itself on first export, and Terraform then fails on a group that
  # already exists. Ours has to land first.
  depends_on = [aws_cloudwatch_log_group.database]

  tags = {
    Name = local.name_prefix
  }
}

# RDS creates this log group itself on first export, with retention set to
# "never expire". Creating it here first means the export lands in a group with a
# retention policy instead of accumulating forever.
resource "aws_cloudwatch_log_group" "database" {
  name              = "/aws/rds/instance/${local.name_prefix}/postgresql"
  retention_in_days = var.log_retention_days
}
