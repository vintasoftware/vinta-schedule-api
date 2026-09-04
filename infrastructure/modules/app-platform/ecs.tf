########################################
# Cluster
########################################

resource "aws_ecs_cluster" "this" {
  name = local.name_prefix

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

locals {
  worker_capacity_provider = var.use_fargate_spot_for_workers ? "FARGATE_SPOT" : "FARGATE"
}

########################################
# Logs
########################################

resource "aws_cloudwatch_log_group" "web" {
  name              = "/ecs/${local.name_prefix}/web"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${local.name_prefix}/worker"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "beat" {
  name              = "/ecs/${local.name_prefix}/beat"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "release" {
  name              = "/ecs/${local.name_prefix}/release"
  retention_in_days = var.log_retention_days
}

########################################
# Container configuration shared by every task definition
########################################

locals {
  # Deliberately absent: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY. boto3 checks
  # the environment before the container credential endpoint, so setting them
  # would override the task role that grants S3 and SQS access.
  container_environment = merge(
    {
      DJANGO_SETTINGS_MODULE = var.django_settings_module

      ALLOWED_HOSTS        = join(",", var.allowed_hosts)
      SITE_DOMAIN          = var.site_domain
      API_DOMAIN           = "https://${var.api_domain}"
      FRONTEND_BASE_URL    = var.frontend_base_url
      CORS_ALLOWED_ORIGINS = join(",", var.cors_allowed_origins)

      DEFAULT_FROM_EMAIL = var.default_from_email
      DEFAULT_BCC_EMAILS = join(",", var.default_bcc_emails)

      # Celery over SQS. `sqs://` carries no credentials on purpose -- kombu falls
      # through to boto3's default chain, which resolves the task role.
      CELERY_BROKER_URL         = "sqs://"
      CELERY_SQS_QUEUE_URL      = aws_sqs_queue.celery.url
      CELERY_TASK_DEFAULT_QUEUE = aws_sqs_queue.celery.name
      # CELERY_SQS_IS_SECURE is left at its default (true). Only local development
      # sets it, because Floci serves plain HTTP.

      # Must stay at or below the queue's own visibility timeout: celery uses this
      # to decide when to extend a message it is still working on.
      CELERY_SQS_VISIBILITY_TIMEOUT = tostring(var.sqs_visibility_timeout_seconds)
      CELERY_SQS_WAIT_TIME_SECONDS  = "20"
      CELERY_SQS_POLLING_INTERVAL   = "1"

      CELERY_WORKER_CONCURRENCY         = tostring(var.worker_concurrency)
      CELERY_WORKER_PREFETCH_MULTIPLIER = "1"
      CELERY_WORKER_MAX_TASKS_PER_CHILD = "1000"
      # Per-child ceiling in KB, held to 60% of the task's memory split across the
      # children. A child that grows past it is retired; without it a leak
      # eventually OOM-kills the container and takes its siblings' work along.
      CELERY_WORKER_MAX_MEMORY_PER_CHILD = tostring(
        floor(var.worker_memory * 1024 * 0.6 / var.worker_concurrency)
      )
      CELERY_TASK_ACKS_ON_FAILURE_OR_TIMEOUT = "true"
      CELERY_TASK_REJECT_ON_WORKER_LOST      = "false"
      # SQS has no fanout exchange, so celery's event stream has nowhere to go.
      CELERY_WORKER_SEND_TASK_EVENTS = "false"

      AWS_REGION = var.aws_region

      AWS_MEDIA_BUCKET_NAME      = local.media_bucket
      AWS_MEDIA_REGION           = var.aws_region
      AWS_MEDIA_S3_ENDPOINT_URL  = "https://s3.${var.aws_region}.amazonaws.com"
      AWS_MEDIA_S3_CUSTOM_DOMAIN = var.media_custom_domain

      AWS_STATIC_BUCKET_NAME      = local.static_bucket
      AWS_STATIC_REGION           = var.aws_region
      AWS_STATIC_S3_CUSTOM_DOMAIN = var.static_custom_domain

      ACCOUNT_PHONE_VERIFICATION_ENABLED = tostring(var.account_phone_verification_enabled)
      DEFAULT_PAYMENT_PROVIDER           = var.default_payment_provider
      BILLING_DEFAULT_GRACE_PERIOD_DAYS  = tostring(var.billing_default_grace_period_days)
    },
    var.extra_environment,
  )

  # Sorted so a plan diff reflects real changes rather than map ordering.
  container_environment_list = [
    for key in sort(keys(local.container_environment)) : {
      name  = key
      value = local.container_environment[key]
    }
  ]

  # `arn:...:secret:name-AbCdEf:JSON_KEY::` -- ECS pulls one key out of the JSON
  # document and injects it as that env var. The two trailing colons are the
  # (empty) version-stage and version-id fields; omitting them makes ECS read the
  # whole document instead.
  container_secrets = [
    for key in sort(local.secret_keys) : {
      name      = key
      valueFrom = "${aws_secretsmanager_secret.app.arn}:${key}::"
    }
  ]

  image = "${aws_ecr_repository.app.repository_url}:latest"
}

########################################
# Task definitions
#
# `image` is a placeholder. GitHub Actions registers a new revision carrying the
# real commit-tagged image on every deploy, which is why the services below ignore
# changes to `task_definition`: Terraform owns the shape, CI owns the tag.
#
# The two do not fight. Terraform's resource is bound to the revision it created,
# and the extra revisions CI registers leave that one untouched -- so a plan shows
# no drift. Changing anything here (an env var, cpu, memory) makes Terraform
# register a revision of its own, which becomes the family's latest ACTIVE one and
# so the base the next deploy copies. The practical consequence: a configuration
# change reaches running containers on the NEXT DEPLOY, not on apply. To pick it up
# immediately, run `aws ecs update-service --force-new-deployment`.
########################################

resource "aws_ecs_task_definition" "web" {
  family                   = "${local.name_prefix}-web"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.web_cpu
  memory                   = var.web_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name      = "web"
      image     = local.image
      essential = true

      command = [
        "gunicorn",
        "vinta_schedule_api.wsgi:application",
        "--bind", "0.0.0.0:${var.container_port}",
        "--workers", tostring(var.gunicorn_workers),
        # Matches the ALB's own 8KB header allowance; the default 4094 rejects the
        # long signed URLs this API hands out.
        "--limit-request-line", "8188",
        "--access-logfile", "-",
        "--error-logfile", "-",
      ]

      portMappings = [
        {
          containerPort = var.container_port
          protocol      = "tcp"
        },
      ]

      environment = local.container_environment_list
      secrets     = local.container_secrets

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.web.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "web"
        }
      }
    },
  ])

}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name_prefix}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name      = "worker"
      image     = local.image
      essential = true

      # Concurrency and prefetch come from CELERY_WORKER_* in the environment
      # rather than flags, so there is one place to change them.
      command = [
        "celery",
        "--app=vinta_schedule_api",
        "worker",
        "--loglevel=info",
      ]

      environment = local.container_environment_list
      secrets     = local.container_secrets

      # SIGTERM makes celery finish its in-flight task before exiting; without it
      # a deploy leaves half-run tasks to reappear after the visibility timeout.
      stopTimeout = 120

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.worker.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "worker"
        }
      }
    },
  ])

}

resource "aws_ecs_task_definition" "beat" {
  family                   = "${local.name_prefix}-beat"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.beat_cpu
  memory                   = var.beat_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name      = "beat"
      image     = local.image
      essential = true

      command = [
        "celery",
        "--app=vinta_schedule_api",
        "beat",
        "--loglevel=info",
      ]

      environment = local.container_environment_list
      secrets     = local.container_secrets

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.beat.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "beat"
        }
      }
    },
  ])

}

# Run once per deploy, before the services roll. Not a service -- CI starts it
# with `ecs run-task` and fails the deploy on a non-zero exit code, so a broken
# migration never reaches the running containers.
resource "aws_ecs_task_definition" "release" {
  family                   = "${local.name_prefix}-release"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name      = "release"
      image     = local.image
      essential = true

      command = [
        "sh", "-c",
        "python manage.py migrate --noinput && python manage.py collectstatic --noinput",
      ]

      environment = local.container_environment_list
      secrets     = local.container_secrets

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.release.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "release"
        }
      }
    },
  ])

}

########################################
# Services
########################################

resource "aws_ecs_service" "web" {
  name            = "${local.name_prefix}-web"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.web.arn
  desired_count   = var.web_desired_count

  platform_version       = "LATEST"
  enable_execute_command = true

  # Web stays on on-demand Fargate: a Spot reclamation here is a user-visible
  # error, not a task that quietly returns to the queue.
  capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.web.arn
    container_name   = "web"
    container_port   = var.container_port
  }

  # Django's import-time work plus the first health check pass; below this the
  # service would kill a task that was merely still booting.
  health_check_grace_period_seconds = 60

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    # CI owns the revision (see the task definition comment); an operator scaling
    # the service in the console should not be undone by the next apply.
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [
    aws_lb_listener.https,
    aws_iam_role_policy.task_execution_secrets,
  ]
}

resource "aws_ecs_service" "worker" {
  name            = "${local.name_prefix}-worker"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_desired_count

  platform_version       = "LATEST"
  enable_execute_command = true

  capacity_provider_strategy {
    capacity_provider = local.worker_capacity_provider
    weight            = 1
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = false
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [aws_iam_role_policy.task_execution_secrets]
}

resource "aws_ecs_service" "beat" {
  name            = "${local.name_prefix}-beat"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.beat.arn

  # Exactly one scheduler. redbeat's Redis lock already stops a second instance
  # from double-emitting, but running one is the intent.
  desired_count = 1

  platform_version       = "LATEST"
  enable_execute_command = true

  capacity_provider_strategy {
    capacity_provider = local.worker_capacity_provider
    weight            = 1
  }

  # Stop the old scheduler before starting the new one, so a deploy never has two
  # holding the schedule at once.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = false
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [aws_iam_role_policy.task_execution_secrets]
}

########################################
# Deploy metadata for CI
#
# Everything scripts/deploy_ecs.sh needs, in one Standard-tier (free) parameter.
# The alternative -- a dozen GitHub repository variables -- drifts the moment
# Terraform renames anything.
########################################

resource "aws_ssm_parameter" "deploy" {
  name        = "/${var.project_name}/${var.environment}/deploy"
  description = "Cluster, services and network wiring the GitHub Actions deploy reads."
  type        = "String"
  tier        = "Standard"

  value = jsonencode({
    cluster             = aws_ecs_cluster.this.name
    region              = var.aws_region
    ecr_repository_url  = aws_ecr_repository.app.repository_url
    release_task_family = aws_ecs_task_definition.release.family
    release_log_group   = aws_cloudwatch_log_group.release.name
    services = [
      {
        name        = aws_ecs_service.web.name
        family      = aws_ecs_task_definition.web.family
        launch_type = "FARGATE"
      },
      {
        name        = aws_ecs_service.worker.name
        family      = aws_ecs_task_definition.worker.family
        launch_type = local.worker_capacity_provider
      },
      {
        name        = aws_ecs_service.beat.name
        family      = aws_ecs_task_definition.beat.family
        launch_type = local.worker_capacity_provider
      },
    ]
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.ecs_tasks.id]
  })
}
