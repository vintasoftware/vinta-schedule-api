from .base import *


# Enable DEBUG in tests so VirtualModelSerializer's query-budget guard is active
# and N+1 regressions fail the suite instead of only surfacing on the dev runtime.
DEBUG = True

SECRET_KEY = "test-secret-key-not-for-production-use-only-0123456789"  # nosec

STATIC_ROOT = base_dir_join("staticfiles")
STATIC_URL = "/static/"

MEDIA_ROOT = base_dir_join("mediafiles")

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

# Redis is optional. Tests must not depend on (or spin up) a Redis server: with an empty
# REDIS_URL the rate limiters transparently use their in-process bucket fallback and
# django-defender is disabled. Keeps CI free of a Redis service.
REDIS_URL = ""
PUBLIC_API_REDIS_URL = ""
CELERY_RESULT_BACKEND = None
