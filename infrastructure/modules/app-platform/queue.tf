########################################
# SQS -- the Celery broker
#
# One standard queue plus a dead-letter queue. The app publishes to a single
# default queue (no task_routes anywhere in the codebase), so a second queue
# would only be dead weight until routing exists.
########################################

resource "aws_sqs_queue" "celery_dlq" {
  name = "${local.name_prefix}-celery-dlq"

  # Two weeks, the SQS maximum: a poison message should still be there on Monday
  # when someone goes looking for why a task never ran.
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true

  tags = {
    Name = "${local.name_prefix}-celery-dlq"
  }
}

resource "aws_sqs_queue" "celery" {
  name = "${local.name_prefix}-celery"

  visibility_timeout_seconds = var.sqs_visibility_timeout_seconds
  message_retention_seconds  = 1209600
  sqs_managed_sse_enabled    = true

  # Long polling. Without it every idle worker poll is a billed API call that
  # returns nothing; with it a poll waits up to 20s for work to arrive.
  receive_wait_time_seconds = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.celery_dlq.arn
    maxReceiveCount     = var.sqs_max_receive_count
  })

  tags = {
    Name = "${local.name_prefix}-celery"
  }
}

resource "aws_sqs_queue_redrive_allow_policy" "celery_dlq" {
  queue_url = aws_sqs_queue.celery_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.celery.arn]
  })
}
