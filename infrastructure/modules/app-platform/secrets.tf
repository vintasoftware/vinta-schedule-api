########################################
# The one secret
#
# A single Secrets Manager secret holds every credential the app needs, as a flat
# JSON object. Each task definition maps one JSON key to one env var
# (`arn:...:KEY::`), so the containers never see the other keys and nothing has to
# parse JSON at boot.
#
# Ownership is split on purpose:
#   * Terraform seeds the initial version -- it knows DATABASE_URL, REDIS_URL and
#     can generate SECRET_KEY / SALT_KEY, so the first deploy boots without a human
#     in the loop.
#   * `ignore_changes` then hands the value over to operators. Filling in the SMTP,
#     Twilio, Stripe, MercadoPago and Google credentials is a console (or CLI) edit,
#     not a Terraform run, and a later `terragrunt apply` will not stomp on it.
#
# Consequence worth knowing: after the first apply, a change to DATABASE_URL or
# REDIS_URL (a restored database, a rebuilt cache) does NOT propagate on its own.
# The `database_url` / `redis_url` outputs exist so you can paste the new value in.
########################################

resource "random_password" "django_secret_key" {
  length  = 64
  special = false
}

resource "random_password" "salt_key" {
  # SALT_KEY drives Fernet at-rest encryption (django-fernet-encrypted-fields).
  # Rotating it makes every already-encrypted field unreadable, which is why it is
  # generated once here and then left alone by `ignore_changes` below.
  length  = 50
  special = false
}

locals {
  # Credentials an operator must supply. Seeded empty so the container still gets
  # the env var -- several settings read these with `config("X")` and no default,
  # which raises when the variable is absent but is happy with an empty string.
  operator_secret_keys = concat([
    "SENTRY_DSN",
    "SMTP_HOST",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_API_KEY_SID",
    "TWILIO_API_KEY_SECRET",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_NUMBER",
    "TWILIO_DEFAULT_BROADCAST_NUMBERS",
    "MERCADOPAGO_ACCESS_TOKEN",
    "MERCADOPAGO_WEBHOOK_SECRET",
    "MERCADOPAGO_PUBLIC_KEY",
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "STRIPE_PUBLISHABLE_KEY",
    # The CloudFront signing pair, from the storage stack:
    #   terragrunt output cloudfront_key_id
    #   terragrunt output -raw cloudfront_private_key
    "AWS_CLOUDFRONT_KEY_ID",
    "AWS_CLOUDFRONT_KEY",
  ], var.extra_secret_keys)

  terraform_managed_secret_values = {
    SECRET_KEY   = random_password.django_secret_key.result
    SALT_KEY     = random_password.salt_key.result
    DATABASE_URL = local.database_url
    REDIS_URL    = local.redis_url
  }

  secret_keys = concat(keys(local.terraform_managed_secret_values), local.operator_secret_keys)

  database_url = format(
    "postgres://%s:%s@%s:%s/%s",
    var.db_username,
    random_password.db.result,
    aws_db_instance.this.address,
    aws_db_instance.this.port,
    var.db_name,
  )
}

resource "aws_secretsmanager_secret" "app" {
  name        = "${local.name_prefix}/app"
  description = "Every credential the ${var.environment} Django/Celery containers read."

  # Long enough to undo an accidental delete, short enough that the name is free
  # again within a sprint if the environment is torn down for real.
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id

  secret_string = jsonencode(merge(
    { for key in local.operator_secret_keys : key => "" },
    local.terraform_managed_secret_values,
  ))

  lifecycle {
    # Operators own this value after the first apply -- see the header comment.
    ignore_changes = [secret_string]
  }
}
