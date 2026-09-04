data "aws_caller_identity" "current" {}

locals {
  media_bucket  = var.media_bucket_name != "" ? var.media_bucket_name : "${var.project_name}-${var.environment}-media"
  static_bucket = var.static_bucket_name != "" ? var.static_bucket_name : "${var.project_name}-${var.environment}-static"
}

########################################
# Task execution role
#
# Used by the ECS agent, not by application code: it pulls the image, resolves the
# Secrets Manager keys and opens the log streams -- all before the container
# starts. Application permissions belong on the task role below.
########################################

data "aws_iam_policy_document" "ecs_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task_execution" {
  name               = "${local.name_prefix}-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume_role.json
}

resource "aws_iam_role_policy_attachment" "task_execution_managed" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "task_execution_secrets" {
  statement {
    sid       = "ReadAppSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.app.arn]
  }
}

resource "aws_iam_role_policy" "task_execution_secrets" {
  name   = "read-app-secret"
  role   = aws_iam_role.task_execution.id
  policy = data.aws_iam_policy_document.task_execution_secrets.json
}

########################################
# Task role
#
# What the Django and Celery processes themselves may do. boto3 picks these
# credentials up from the container credential endpoint, which is why no
# AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY is set anywhere: a static key in the
# environment would take precedence and quietly override this role.
########################################

resource "aws_iam_role" "task" {
  name               = "${local.name_prefix}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume_role.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid = "CeleryBroker"
    actions = [
      "sqs:SendMessage",
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
      "sqs:ChangeMessageVisibility",
    ]
    resources = [
      aws_sqs_queue.celery.arn,
      aws_sqs_queue.celery_dlq.arn,
    ]
  }

  statement {
    sid = "Storage"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:GetObjectAcl",
      "s3:PutObjectAcl",
    ]
    resources = [
      "arn:aws:s3:::${local.media_bucket}/*",
      "arn:aws:s3:::${local.static_bucket}/*",
    ]
  }

  statement {
    sid = "StorageListing"
    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = [
      "arn:aws:s3:::${local.media_bucket}",
      "arn:aws:s3:::${local.static_bucket}",
    ]
  }

  # `aws ecs execute-command` -- a shell in a running task. The only way into the
  # private subnets without standing up a bastion, and how you reach a Django
  # shell or psql against this database.
  statement {
    sid = "ExecuteCommand"
    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "app"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

########################################
# GitHub Actions deploy role (OIDC)
#
# No long-lived keys in GitHub: the workflow exchanges its OIDC token for this
# role. The trust policy pins both the repository and the ref, so a workflow run
# from a fork or a feature branch cannot assume it.
########################################

resource "aws_iam_openid_connect_provider" "github" {
  count = var.github_oidc_provider_arn == "" ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # GitHub's OIDC endpoint now presents a certificate chained to a well-known root,
  # and IAM no longer verifies this list -- AWS's own docs keep this value as the
  # placeholder it has become.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

locals {
  github_oidc_provider_arn = (
    var.github_oidc_provider_arn != ""
    ? var.github_oidc_provider_arn
    : aws_iam_openid_connect_provider.github[0].arn
  )
}

data "aws_iam_policy_document" "github_assume_role" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:ref:${var.github_deploy_ref}"]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  name               = "${local.name_prefix}-github-deploy"
  description        = "Assumed by ${var.github_repository} on ${var.github_deploy_ref} to build, push and roll out."
  assume_role_policy = data.aws_iam_policy_document.github_assume_role.json
}

data "aws_iam_policy_document" "github_deploy" {
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.app.arn]
  }

  statement {
    sid = "ReadDeployMetadata"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
    ]
    resources = [aws_ssm_parameter.deploy.arn]
  }

  # `RegisterTaskDefinition` and `DescribeTaskDefinition` take no resource ARN --
  # IAM only accepts "*" for them. The blast radius is bounded by the PassRole
  # statement below: a registered definition is inert unless it can pass this
  # environment's two roles.
  statement {
    sid = "TaskDefinitions"
    actions = [
      "ecs:DescribeTaskDefinition",
      "ecs:RegisterTaskDefinition",
    ]
    resources = ["*"]
  }

  statement {
    sid = "RolloutServices"
    actions = [
      "ecs:DescribeServices",
      "ecs:UpdateService",
    ]
    resources = [
      aws_ecs_service.web.id,
      aws_ecs_service.worker.id,
      aws_ecs_service.beat.id,
    ]
  }

  statement {
    sid = "RunRelease"
    actions = [
      "ecs:RunTask",
      "ecs:DescribeTasks",
      "ecs:StopTask",
    ]
    resources = ["*"]

    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.this.arn]
    }
  }

  statement {
    sid     = "PassTaskRoles"
    actions = ["iam:PassRole"]
    resources = [
      aws_iam_role.task_execution.arn,
      aws_iam_role.task.arn,
    ]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }

  # So a failed migration prints its traceback in the workflow log instead of
  # sending someone to the console.
  statement {
    sid = "ReadReleaseLogs"
    actions = [
      "logs:GetLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = ["${aws_cloudwatch_log_group.release.arn}:*"]
  }
}

resource "aws_iam_role_policy" "github_deploy" {
  name   = "deploy"
  role   = aws_iam_role.github_deploy.id
  policy = data.aws_iam_policy_document.github_deploy.json
}
