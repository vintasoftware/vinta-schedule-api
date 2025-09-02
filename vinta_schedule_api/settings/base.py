import os
from collections.abc import Callable
from datetime import timedelta
from urllib.parse import quote

from decouple import Csv, config  # type: ignore
from dj_database_url import parse as db_url


BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def base_dir_join(*args):
    return os.path.join(BASE_DIR, *args)


SITE_ID = 1

DEBUG = True

ADMINS = (("Admin", "foo@example.com"),)

AUTH_USER_MODEL = "users.User"

ALLOWED_HOSTS: list[str] = []

DATABASES = {
    "default": config("DATABASE_URL", cast=db_url),
}
INTERNAL_INSTALLED_APPS = [
    "di_core",
    "common",
    "s3direct_overrides",
    "accounts",
    "users",
    "organizations",
    "payments",
    "notifications",
    "calendar_integration",
    "webhooks",
    "public_api",
]
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "django.contrib.sites",
    "corsheaders",
    "import_export",
    "rest_framework",
    "drf_spectacular",
    "defender",
    "django_guid",
    "allauth",
    "allauth.headless",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "allauth.socialaccount.providers.apple",
    "allauth.mfa",
    "rest_framework.authtoken",
    "django_filters",
    "vintasend_django",
    "s3direct",
    *INTERNAL_INSTALLED_APPS,
]

MIDDLEWARE = [
    "django.middleware.gzip.GZipMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django_permissions_policy.PermissionsPolicyMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "public_api.middlewares.PublicApiSystemUserMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "csp.middleware.CSPMiddleware",
    "defender.middleware.FailedLoginMiddleware",
    "django_guid.middleware.guid_middleware",
    "allauth.account.middleware.AccountMiddleware",
]

ROOT_URLCONF = "vinta_schedule_api.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [base_dir_join("templates")],
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
            "loaders": [
                (
                    "django.template.loaders.cached.Loader",
                    [
                        "django.template.loaders.filesystem.Loader",
                        "django.template.loaders.app_directories.Loader",
                    ],
                ),
            ],
        },
    },
]

WSGI_APPLICATION = "vinta_schedule_api.wsgi.application"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

REST_FRAMEWORK = {
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 10,
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTStatelessUserAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": ("django_filters.rest_framework.DjangoFilterBackend",),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True


USE_TZ = True

REDIS_URL = config("REDIS_URL")

# Celery
# Recommended settings for reliability: https://gist.github.com/fjsj/da41321ac96cf28a96235cb20e7236f6
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TASK_ACKS_LATE = True
CELERY_TIMEZONE = TIME_ZONE
CELERY_BROKER_TRANSPORT_OPTIONS = {"confirm_publish": True, "confirm_timeout": 5.0}
CELERY_BROKER_POOL_LIMIT = config("CELERY_BROKER_POOL_LIMIT", cast=int, default=1)
CELERY_BROKER_CONNECTION_TIMEOUT = config(
    "CELERY_BROKER_CONNECTION_TIMEOUT", cast=float, default=30.0
)
CELERY_REDIS_MAX_CONNECTIONS = config(
    "CELERY_REDIS_MAX_CONNECTIONS", cast=lambda v: int(v) if v else None, default=None
)
CELERY_TASK_ACKS_ON_FAILURE_OR_TIMEOUT = config(
    "CELERY_TASK_ACKS_ON_FAILURE_OR_TIMEOUT", cast=bool, default=True
)
CELERY_TASK_REJECT_ON_WORKER_LOST = config(
    "CELERY_TASK_REJECT_ON_WORKER_LOST", cast=bool, default=False
)
CELERY_WORKER_PREFETCH_MULTIPLIER = config("CELERY_WORKER_PREFETCH_MULTIPLIER", cast=int, default=1)
CELERY_WORKER_CONCURRENCY = config(
    "CELERY_WORKER_CONCURRENCY", cast=lambda v: int(v) if v else None, default=None
)
CELERY_WORKER_MAX_TASKS_PER_CHILD = config(
    "CELERY_WORKER_MAX_TASKS_PER_CHILD", cast=int, default=1000
)
CELERY_WORKER_SEND_TASK_EVENTS = config("CELERY_WORKER_SEND_TASK_EVENTS", cast=bool, default=True)
CELERY_EVENT_QUEUE_EXPIRES = config("CELERY_EVENT_QUEUE_EXPIRES", cast=float, default=60.0)
CELERY_EVENT_QUEUE_TTL = config("CELERY_EVENT_QUEUE_TTL", cast=float, default=5.0)

# Sentry
SENTRY_DSN = config("SENTRY_DSN", default="")
COMMIT_SHA = config("RENDER_GIT_COMMIT", default="")

# Fix for Safari 12 compatibility issues, please check:
# https://github.com/vintasoftware/safari-samesite-cookie-issue
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SAMESITE = "Lax"

# All available policies are listed at:
# https://github.com/w3c/webappsec-permissions-policy/blob/main/features.md
# Empty list means the policy is disabled
PERMISSIONS_POLICY: dict[str, list] = {
    "accelerometer": [],
    "camera": [],
    "display-capture": [],
    "encrypted-media": [],
    "geolocation": [],
    "gyroscope": [],
    "magnetometer": [],
    "microphone": [],
    "midi": [],
    "payment": [],
    "usb": [],
    "xr-spatial-tracking": [],
}

# Django-CSP
CSP_INCLUDE_NONCE_IN = ["script-src", "style-src", "font-src"]
CSP_SCRIPT_SRC = [
    "'self'",
    "'unsafe-inline'",
    "'unsafe-eval'",
    "https://browser.sentry-cdn.com",
    # drf-spectacular UI (Swagger and ReDoc)
    "https://cdn.jsdelivr.net/npm/swagger-ui-dist@latest/",
    "https://cdn.jsdelivr.net/npm/redoc@latest/",
    "blob:",
] + [f"*{host}" if host.startswith(".") else host for host in ALLOWED_HOSTS]
CSP_CONNECT_SRC = [
    "'self'",
    "*.sentry.io",
] + [f"*{host}" if host.startswith(".") else host for host in ALLOWED_HOSTS]
CSP_STYLE_SRC = [
    "'self'",
    "'unsafe-inline'",
    # drf-spectacular UI (Swagger and ReDoc)
    "https://cdn.jsdelivr.net/npm/swagger-ui-dist@latest/",
    "https://cdn.jsdelivr.net/npm/redoc@latest/",
    "https://fonts.googleapis.com",
]
CSP_FONT_SRC = [
    "'self'",
    "'unsafe-inline'",
    # drf-spectacular UI (Swagger and ReDoc)
    "https://fonts.gstatic.com",
] + [f"*{host}" if host.startswith(".") else host for host in ALLOWED_HOSTS]
CSP_IMG_SRC = [
    "'self'",
    # drf-spectacular UI (Swagger and ReDoc)
    "data:",
    "https://cdn.jsdelivr.net/npm/swagger-ui-dist@latest/",
    "https://cdn.redoc.ly/redoc/",
]

# Django-defender
DEFENDER_LOGIN_FAILURE_LIMIT = 3
DEFENDER_COOLOFF_TIME = 300  # 5 minutes
DEFENDER_LOCKOUT_TEMPLATE = "defender/lockout.html"
DEFENDER_REDIS_URL = config("REDIS_URL")

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=5),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,  # IMPORTANT
    "BLACKLIST_AFTER_ROTATION": True,  # IMPORTANT
    "UPDATE_LAST_LOGIN": True,
}

SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "SCOPE": [
            "openid",
            "profile",
            "email",
        ],
        "AUTH_PARAMS": {
            "access_type": "online",
        },
    }
}

CORS_ORIGIN_ALLOW_ALL = True
from corsheaders.defaults import default_headers


CORS_ALLOW_HEADERS = (
    *default_headers,
    "x-session-token",
    "x-email-verification-key",
    "x-password-reset-key",
)
CORS_ALLOW_CREDENTIALS = True
HEADLESS_ONLY = True
ACCOUNT_USER_MODEL_USERNAME_FIELD = "username"
ACCOUNT_LOGIN_METHODS = {"email", "username", "phone"}
ACCOUNT_SIGNUP_FIELDS = [
    "username*",
    "email*",
    "phone*",
    "password1",
    "password2",
    "first_name*",
    "last_name*",
]
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_ADAPTER = "accounts.account_adapters.SocialAccountAdapter"
ACCOUNT_ADAPTER = "accounts.account_adapters.AccountAdapter"
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_EMAIL_VERIFICATION_BY_CODE_ENABLED = True
SOCIALACCOUNT_EMAIL_VERIFICATION = None
HEADLESS_FRONTEND_URLS = {
    "account_confirm_email": "http://localhost:3000/account/verify-email/{key}",
    "account_reset_password": "http://localhost:3000/account/password/reset",
    "account_reset_password_from_key": "http://localhost:3000/reset-password/{key}",
    "account_signup": "http://localhost:3000/account/signup",
    "socialaccount_login_error": "http://localhost:3000/account/provider/callback",
}
MFA_SUPPORTED_TYPES = ["totp", "recovery_codes"]
MFA_PASSKEY_LOGIN_ENABLED = False
HEADLESS_SERVE_SPECIFICATION = True
HEADLESS_CLIENTS = ("app", "browser")
HEADLESS_ADAPTER = "accounts.account_adapters.HeadlessAdapter"
HEADLESS_TOKEN_STRATEGY = "accounts.token_strategies.AccessAndRefreshTokenStrategy"  # noqa: S105
ACCESS_TOKEN_EXPIRY_MINUTES = config("ACCESS_TOKEN_EXPIRY_MINUTES", cast=int, default=15)
REFRESH_TOKEN_EXPIRY_DAYS = config("REFRESH_TOKEN_EXPIRY_DAYS", cast=int, default=30)
ACCOUNT_LOGIN_METHODS = {"phone", "email", "username"}
ACCOUNT_PHONE_VERIFICATION_ENABLED = True
ACCOUNT_PHONE_VERIFICATION_MAX_ATTEMPTS = 3
ACCOUNT_PHONE_VERIFICATION_SUPPORTS_RESEND = True
ACCOUNT_SIGNUP_FORM_CLASS = "accounts.base_forms.BaseVintaScheduleSignupForm"


SPECTACULAR_SETTINGS = {
    "TITLE": "Vinta Schedule API",
    "DESCRIPTION": "API for vinta-schedule-api project",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "PREPROCESSING_HOOKS": [],
    "ENUM_ADD_EXPLICIT_BLANK_NULL_CHOICE": False,
    "ENUM_NAME_OVERRIDES": {
        "FrequencyEnum": "calendar_integration.constants.RecurrenceFrequency.choices",
        "RSVPStatusEnum": "calendar_integration.constants.RSVPStatus.choices",
    },
}


def is_valid_url(s):
    from django.core.exceptions import ValidationError

    # pylint: disable=import-outside-toplevel
    from django.core.validators import URLValidator

    if not s:
        return False

    val = URLValidator()
    try:
        val(s)
    except ValidationError:
        return False

    return True


def append_uuid_to_filename(filename):
    import os

    from cuid2 import cuid_wrapper

    cuid_generator: Callable[[], str] = cuid_wrapper()

    filename_without_ext, ext = os.path.splitext(filename)
    return f"{filename_without_ext}_{cuid_generator()}{ext}"


def generate_s3direct_file_name(original_file_name, dest):
    import re

    no_special_chars_file_name = re.sub(r"[^a-zA-Z0-9\\.]", "_", original_file_name)
    unique_file_name = append_uuid_to_filename(no_special_chars_file_name)

    if not is_valid_url(f"https://example.com/{dest}/{unique_file_name}"):
        return f"{dest}/{quote(unique_file_name)}"

    return f"{dest}/{unique_file_name}"


S3DIRECT_DESTINATIONS = {
    "profile_pictures": {
        "key": generate_s3direct_file_name,
        "key_args": "uploads/profile_pictures",
        "auth": lambda u: u.is_authenticated,
        "acl": "private",
    },
}

SOCIALACCOUNT_PROVIDERS = {}
if config("APPLE_SERVICE_ID", default=""):
    SOCIALACCOUNT_PROVIDERS["apple"] = {
        "APPS": [
            {
                # Your service identifier.
                "client_id": config("APPLE_SERVICE_ID", default=""),
                # The Key ID (visible in the "View Key Details" page).
                "secret": config("APPLE_KEY_ID", default=""),
                # Member ID/App ID Prefix -- you can find it below your name
                # at the top right corner of the page, or it's your App ID
                # Prefix in your App ID.
                "key": config("APPLE_MEMBER_APP_ID_PREFIX", default=""),
                "settings": {
                    # The certificate you downloaded when generating the key.
                    "certificate_key": config("APPLE_CERTIFICATE_KEY", default=""),
                },
            }  # type: ignore
        ]
    }
if config("FACEBOOK_APP_ID", default=""):
    SOCIALACCOUNT_PROVIDERS["facebook"] = {
        "APPS": [
            {
                "client_id": config("FACEBOOK_APP_ID", default=""),
                "secret": config("FACEBOOK_APP_SECRET", default=""),
            }  # type: ignore
        ],
        "SCOPE": ["email"],
        "AUTH_PARAMS": {"auth_type": "reauthenticate"},
    }
if config("GOOGLE_CLIENT_ID", default=""):
    SOCIALACCOUNT_PROVIDERS["google"] = {
        "APPS": [
            {
                "client_id": config("GOOGLE_CLIENT_ID", default=""),
                "secret": config("GOOGLE_CLIENT_SECRET", default=""),
                "key": "",
            },  # type: ignore
        ],
        "SCOPE": [
            "openid",
            "profile",
            "email",
        ],
        "AUTH_PARAMS": {
            "access_type": "online",
        },
        # "CERTS_URL": "https://www.googleapis.com/oauth2/v3/certs"
    }

SITE_DOMAIN = config("SITE_DOMAIN", default="localhost:8000")
API_DOMAIN = config("API_DOMAIN", default="localhost:3000")
DEFAULT_BCC_EMAILS: list[str] = config("DEFAULT_BCC_EMAILS", default=[], cast=Csv())
DEFAULT_PROTOCOL = "http"


# SES
SES_CONFIGURATION_SET = "all-emails"


MERCADOPAGO_ACCESS_TOKEN = config("MERCADOPAGO_ACCESS_TOKEN", default="")

SALT_KEY = config("SALT_KEY")

TWILIO_ACCOUNT_SID = config("TWILLIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = config("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER = config("TWILIO_NUMBER")
TWILLIO_DEFAULT_BROADCAST_NUMBERS: list[str] = config(
    "TWILLIO_DEFAULT_BROADCAST_NUMBERS", default=[], cast=Csv()
)

BASE_URL_DOMAIN = config("BASE_URL_DOMAIN", "localhost:8000")
BASE_URL_PROTOCOL = config("BASE_URL_PROTOCOL", "http")
NOTIFICATION_DEFAULT_BASE_URL_DOMAIN = BASE_URL_DOMAIN
NOTIFICATION_DEFAULT_BASE_URL_PROTOCOL = BASE_URL_DOMAIN
BASE_URL = f"{BASE_URL_PROTOCOL}://{BASE_URL_DOMAIN}"

PUBLIC_API_REDIS_URL = config("PUBLIC_API_REDIS_URL", default=REDIS_URL)
PUBLIC_API_REQUESTS_PER_SECOND_LIMIT = config("PUBLIC_API_REQUESTS_PER_SECOND_LIMIT", default=5)
PUBLIC_API_REQUESTS_PER_MINUTE_LIMIT = config("PUBLIC_API_REQUESTS_PER_MINUTE_LIMIT", default=100)
PUBLIC_API_REQUESTS_PER_HOUR_LIMIT = config("PUBLIC_API_REQUESTS_PER_HOUR_LIMIT", default=1000)
PUBLIC_API_RATE_LIMITER_KEY = config("PUBLIC_API_RATE_LIMITER_KEY", default="public_api")
