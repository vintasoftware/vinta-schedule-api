from .base import *


# Enable DEBUG in tests so VirtualModelSerializer's query-budget guard is active
# and N+1 regressions fail the suite instead of only surfacing on the dev runtime.
DEBUG = True

# Add the invitation-accept URLs so sendEmail=false tests receive a non-null inviteUrl.
# The base settings omit these keys; staging/production set them against FRONTEND_BASE_URL.
# Unlike staging/production, these values are plain strings, not f-strings -- the host
# here is a hardcoded literal, so {token}/{org_slug} don't need the {{ }} escaping those
# other settings use to survive f-string interpolation of FRONTEND_BASE_URL. Single braces
# is correct here: build_invitation_accept_url calls .format(token=..., org_slug=...) on
# these templates directly.
HEADLESS_FRONTEND_URLS = {
    **HEADLESS_FRONTEND_URLS,
    "account_accept_invitation": "http://localhost:3000/auth/accept-invite/?token={token}",
    "account_accept_invitation_branded": (
        "http://localhost:3000/o/{org_slug}/auth/accept-invite/?token={token}"
    ),
}

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

# Pinned to fake non-empty values, like SECRET_KEY/SALT_KEY above -- so
# `BasePaymentAdapter.is_configured` (and therefore whether a charge is
# attempted or refused with `PaymentProviderNotConfiguredError`) answers the
# same way regardless of the developer's own `.env`. Every charge-path test
# already overrides the DI adapter slot directly, so this only matters for a
# test that forgets to -- which must fail (or pass) identically on every
# machine, not depending on whether STRIPE_SECRET_KEY happens to be set locally.
STRIPE_SECRET_KEY = "sk_test_fake-not-for-production-use-only"  # nosec
MERCADOPAGO_ACCESS_TOKEN = "test-fake-mercadopago-access-token-not-for-production-use"  # nosec

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
