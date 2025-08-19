import sentry_sdk
from decouple import Csv, config  # type: ignore
from django_guid.integrations import SentryIntegration as DjangoGUIDSentryIntegration
from sentry_sdk.integrations.django import DjangoIntegration

from .base import *


DEBUG = False

SECRET_KEY = config("SECRET_KEY")

DATABASES["default"]["ATOMIC_REQUESTS"] = True

ALLOWED_HOSTS = config("ALLOWED_HOSTS", cast=Csv())

STATIC_ROOT = base_dir_join("staticfiles")
STATIC_URL = "/static/"

MEDIA_ROOT = base_dir_join("mediafiles")
MEDIA_URL = "/media/"

SERVER_EMAIL = "foo@example.com"

EMAIL_HOST = config("SMTP_HOST")
EMAIL_HOST_USER = config("SMTP_USERNAME")
EMAIL_HOST_PASSWORD = config("SMTP_PASSWORD")
EMAIL_PORT = 587
EMAIL_USE_TLS = True

# Security
SECURE_HSTS_PRELOAD = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = config("SECURE_HSTS_SECONDS", default=3600, cast=int)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = "DENY"

# Celery
# Recommended settings for reliability: https://gist.github.com/fjsj/da41321ac96cf28a96235cb20e7236f6
CELERY_BROKER_URL = config("RABBITMQ_URL", default="")
CELERY_RESULT_BACKEND = config("REDIS_URL")
CELERY_SEND_TASK_ERROR_EMAILS = True

# Redbeat https://redbeat.readthedocs.io/en/latest/config.html#redbeat-redis-url
redbeat_redis_url = config("REDBEAT_REDIS_URL", default="")


AWS_REGION = config("AWS_REGION", default="us-east-1")

# Static storage
AWS_STATIC_BUCKET_NAME = config("AWS_STATIC_BUCKET_NAME")
AWS_STATIC_LOCATION = config("AWS_STATIC_LOCATION", default="")
AWS_STATIC_REGION = config("AWS_STATIC_REGION", default="us-east-1")
AWS_STATIC_S3_CUSTOM_DOMAIN = config(
    "AWS_STATIC_S3_CUSTOM_DOMAIN",
    default=f"{AWS_STATIC_BUCKET_NAME}.s3.{AWS_STATIC_REGION}.amazonaws.com",
)
STATIC_URL = (
    f"https://{AWS_STATIC_S3_CUSTOM_DOMAIN}/{AWS_STATIC_LOCATION}"
    if AWS_STATIC_LOCATION
    else f"https://{AWS_STATIC_S3_CUSTOM_DOMAIN}/"
)
BASE_STATIC_URL = f"https://{AWS_STATIC_S3_CUSTOM_DOMAIN}"
FRONTEND_BUNDLE_DIR = config("FRONTEND_BUNDLE_DIR", default="webpack_bundles")

# Media storage
USE_MINIO = False
AWS_MEDIA_BUCKET_NAME = config("AWS_MEDIA_BUCKET_NAME")
AWS_MEDIA_LOCATION = config("AWS_MEDIA_LOCATION", default="")
AWS_MEDIA_REGION = config("AWS_MEDIA_REGION", default="us-east-1")
AWS_MEDIA_S3_CUSTOM_DOMAIN = config(
    "AWS_MEDIA_S3_CUSTOM_DOMAIN",
    default=None,
)
AWS_S3_URL_PROTOCOL = "https:"
AWS_MEDIA_S3_ENDPOINT_URL = config("AWS_MEDIA_S3_ENDPOINT_URL")
AWS_STORAGE_BUCKET_NAME = AWS_MEDIA_BUCKET_NAME
AWS_CLOUDFRONT_KEY_ID = config("AWS_CLOUDFRONT_KEY_ID")
AWS_CLOUDFRONT_KEY = config("AWS_CLOUDFRONT_KEY")
MEDIA_ROOT = "mediafiles"
if AWS_MEDIA_S3_CUSTOM_DOMAIN:
    MEDIA_URL = f"{AWS_S3_URL_PROTOCOL}//{AWS_MEDIA_S3_CUSTOM_DOMAIN}/{AWS_MEDIA_LOCATION + '/' if AWS_MEDIA_LOCATION else ''}"
else:
    MEDIA_URL = (
        f"{AWS_S3_URL_PROTOCOL}//s3.{AWS_MEDIA_REGION}.amazonaws.com/"
        f"{AWS_MEDIA_BUCKET_NAME}/{AWS_MEDIA_LOCATION + '/' if AWS_MEDIA_LOCATION else ''}"
    )


STORAGES = {
    "default": {"BACKEND": "common.media_storage_backend.MediaStorage"},
    "staticfiles": {"BACKEND": "common.static_storage_backend.StaticStorage"},
}

# Django GUID
DJANGO_GUID = {
    "INTEGRATIONS": [
        DjangoGUIDSentryIntegration(),
    ],
}

# django-log-request-id
MIDDLEWARE.insert(  # insert RequestIDMiddleware on the top
    0, "log_request_id.middleware.RequestIDMiddleware"
)

LOG_REQUEST_ID_HEADER = "HTTP_X_REQUEST_ID"
LOG_REQUESTS = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "require_debug_false": {"()": "django.utils.log.RequireDebugFalse"},
        "request_id": {"()": "log_request_id.filters.RequestIDFilter"},
        "correlation_id": {"()": "django_guid.log_filters.CorrelationId"},
    },
    "formatters": {
        "standard": {
            "format": "%(levelname)-8s [%(asctime)s] [%(request_id)s] [%(correlation_id)s] %(name)s: %(message)s"
        },
    },
    "handlers": {
        "null": {
            "class": "logging.NullHandler",
        },
        "mail_admins": {
            "level": "ERROR",
            "class": "django.utils.log.AdminEmailHandler",
            "filters": ["require_debug_false"],
        },
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "filters": ["request_id", "correlation_id"],
            "formatter": "standard",
        },
    },
    "loggers": {
        "": {"handlers": ["console"], "level": "INFO"},
        "django.security.DisallowedHost": {
            "handlers": ["null"],
            "propagate": False,
        },
        "django.request": {
            "handlers": ["mail_admins"],
            "level": "ERROR",
            "propagate": True,
        },
        "log_request_id.middleware": {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
        "django_guid": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}

# Sentry
sentry_sdk.init(dsn=SENTRY_DSN, integrations=[DjangoIntegration()], release=COMMIT_SHA)

HEADLESS_FRONTEND_URLS = {
    "account_confirm_email": "https://schedule.vinta.com.br/verify-email/{key}",
    "account_reset_password": "https://schedule.vinta.com.br/password-reset",
    "account_reset_password_from_key": "https://schedule.vinta.com.br/password-reset/{key}",
    "account_signup": "https://schedule.vinta.com.br/signup",
    "socialaccount_login_error": "https://schedule.vinta.com.br/social-login-error",
}
ACCOUNT_DEFAULT_HTTP_PROTOCOL = "https"


DEFAULT_PROTOCOL = "https"
