from .base import *


SECRET_KEY = "test"  # nosec

STATIC_ROOT = base_dir_join("staticfiles")
STATIC_URL = "/static/"

MEDIA_ROOT = base_dir_join("mediafiles")

USE_MINIO = False
AWS_MEDIA_LOCATION = ""
AWS_MEDIA_S3_CUSTOM_DOMAIN = "media-test.vinta_schedule.com.br"
S3DIRECT_ENDPOINT = "https://s3.us-east-1.amazonaws.com"
AWS_MEDIA_BUCKET_NAME = "media-test-vinta_schedule.com.br"
MEDIA_S3_BASE_URL = f"https://{AWS_MEDIA_S3_CUSTOM_DOMAIN}/"

MEDIA_ROOT = base_dir_join("mediafiles")
MEDIA_URL = MEDIA_S3_BASE_URL

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Speed up password hashing
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# Celery
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

SITE_DOMAIN = "test-schedule.vinta.com.br"
SALT_KEY = "123467890asdfghjkl"

# Disable rate limiting for tests
PUBLIC_API_REQUESTS_PER_SECOND_LIMIT = 0
PUBLIC_API_REQUESTS_PER_MINUTE_LIMIT = 0
PUBLIC_API_REQUESTS_PER_HOUR_LIMIT = 0
