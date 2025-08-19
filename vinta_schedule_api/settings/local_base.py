from .base import *


DEBUG = True

HOST = "http://localhost:8000"

SECRET_KEY = "secret"  # noqa: S105

STATIC_ROOT = base_dir_join("staticfiles")
STATIC_URL = "/static/"

MEDIA_ROOT = base_dir_join("mediafiles")
MEDIA_URL = "/media/"

# Backward compatibility with MinIO (deprecated)
MINIO_ACCESS_KEY = config("MINIO_ROOT_USER", default="test")
MINIO_SECRET_KEY = config("MINIO_ROOT_PASSWORD", default="test")
MINIO_BUCKET_NAME = config("MINIO_BUCKET_NAME", default="vinta_schedule")
MINIO_ENDPOINT = config("MINIO_ENDPOINT", default="http://localstack:4566")

# LocalStack S3 configuration (preferred)
LOCALSTACK_ENDPOINT = config("LOCALSTACK_ENDPOINT", default="http://localstack:4566")
AWS_ACCESS_KEY_ID = config("AWS_ACCESS_KEY_ID", default="test")
AWS_SECRET_ACCESS_KEY = config("AWS_SECRET_ACCESS_KEY", default="test")
S3_BUCKET_NAME = config("S3_BUCKET_NAME", default="vinta_schedule")

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

AUTH_PASSWORD_VALIDATORS = []  # allow easy passwords only on local

# Celery
CELERY_BROKER_URL = config("CELERY_BROKER_URL", default="")
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Email settings for mailhog
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = "mailpit"
EMAIL_PORT = 1025

# Logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "correlation_id": {"()": "django_guid.log_filters.CorrelationId"},
    },
    "formatters": {
        "standard": {
            "format": "%(levelname)-8s [%(asctime)s] [%(correlation_id)s] %(name)s: %(message)s"
        },
    },
    "handlers": {
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "filters": ["correlation_id"],
        },
    },
    "loggers": {
        "": {"handlers": ["console"], "level": "INFO"},
        "celery": {"handlers": ["console"], "level": "INFO"},
        "django_guid": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}

JS_REVERSE_JS_MINIFY = False

# Django-CSP
LOCAL_HOST_URL = "http://localhost:3000"
LOCAL_HOST_WS_URL = "ws://localhost:3000/ws"
LOCALSTACK_URL = "http://localhost:4566"
CSP_SCRIPT_SRC += [LOCAL_HOST_URL, LOCAL_HOST_WS_URL, LOCALSTACK_URL]
CSP_CONNECT_SRC += [LOCAL_HOST_URL, LOCAL_HOST_WS_URL, LOCALSTACK_URL]
CSP_FONT_SRC += [LOCAL_HOST_URL, LOCALSTACK_URL]
CSP_IMG_SRC += [LOCAL_HOST_URL, LOCALSTACK_URL]
