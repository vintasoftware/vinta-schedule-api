import os
from collections.abc import Callable
from datetime import timedelta
from urllib.parse import quote

from decouple import Csv, config  # type: ignore
from dj_database_url import parse as db_url

from payments.provider_slugs import PAYMENT_PROVIDER_SLUGS


BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def base_dir_join(*args):
    return os.path.join(BASE_DIR, *args)


SITE_ID = 1

DEBUG = True

ADMINS = ["hugo@vinta.com.br"]

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
    "tenancy",
    "audit",
    "payments",
    "notifications",
    "calendar_integration",
    "webhooks",
    "legal",
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
    "django.contrib.postgres",
    "corsheaders",
    "import_export",
    "rest_framework",
    "drf_spectacular",
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
    # vinta-django-orgs' own Django app. Deliberately NOT in
    # INTERNAL_INSTALLED_APPS: that list drives di_core's DI wiring
    # (``container.wire(packages=INTERNAL_INSTALLED_APPS)``) and names only this
    # project's apps. Installed for its abstract bases, its managers, and the
    # ``class_prepared`` index receiver; ``OrganizationSite`` (its one
    # non-swappable model) gets a table that stays empty and unread -- see the
    # plan's Non-goals.
    "organizations.apps.OrganizationsConfig",
    *INTERNAL_INSTALLED_APPS,
]

# ``Organization`` and ``OrganizationMembership`` are swappable the way
# ``auth.User`` is, and Django reads ``Meta.swappable`` through a *top-level*
# setting (a plain ``getattr(settings, "ORGANIZATION_MODEL")``) -- so these two
# cannot live inside SHARED_SCHEMA_ORGANIZATIONS below. Pointing them at our
# models marks the package's own concrete models swapped out: no table is
# created for them, no phantom CASCADE relation hangs off ``User.delete()``,
# and there is no ``models.E028`` duplicate-``db_table`` collision with ours.
ORGANIZATION_MODEL = "tenancy.Organization"
ORGANIZATION_MEMBERSHIP_MODEL = "tenancy.OrganizationMembership"

SHARED_SCHEMA_ORGANIZATIONS = {
    # Our ``X-Organization-Id`` retriever, and only ours. The package's
    # ``retrieve_by_domain`` / ``retrieve_by_http_header`` (Organization-Slug) /
    # ``retrieve_by_session`` are all plan Non-goals: we do not do subdomain
    # tenancy, slug-header resolution, or session-pinned organizations.
    "ORGANIZATION_RETRIEVERS": ["common.org_retrievers.retrieve_by_x_organization_id"],
    # No catch-all organization. The package's default is the slug "default",
    # which ``SingleOrganizationModelMixin.save()`` falls back to when a scoped
    # row is saved with no organization set and none bound. We have no such
    # organization (``default`` is a reserved slug -- see
    # ``tenancy.slug_validation``), and silently filing a row under one would be
    # exactly the cross-tenant write the whole migration exists to prevent.
    # ``None`` makes the fallback a no-op instead of a wasted query.
    "DEFAULT_ORGANIZATION_SLUG": None,
    # A query against a scoped model with no organization bound raises
    # ``OrganizationNotFoundError`` instead of quietly returning nothing.
    # The package defaults this off because an unscoped *read* leaks nothing --
    # but "returns no rows" and "forgot to bind an organization" are
    # indistinguishable in a Celery task or a management command, which is
    # exactly where this codebase's scoped reads run furthest from a request.
    # Loud is the point: every call site that must cross organizations says so
    # with ``original_manager`` / ``unscoped()``, and every call site that knows
    # its organization says so with ``filter_by_organization(...)`` (which
    # bypasses the context by design). What is left over is the bug.
    "STRICT_ORGANIZATION_FILTER": True,
}

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
    "django_guid.middleware.guid_middleware",
    "allauth.account.middleware.AccountMiddleware",
    # After AuthenticationMiddleware (needs request.user) -- see the class docstring.
    "accounts.middlewares.PostAuthDestinationMiddleware",
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
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": ("django_filters.rest_framework.DjangoFilterBackend",),
    "DEFAULT_SCHEMA_CLASS": "common.openapi.TenantScopedAutoSchema",
    # Delegates to DRF's default handler for everything except the domain
    # exceptions it explicitly knows about (currently `OverLimitError` -> 402),
    # so no existing error rendering changes.
    "EXCEPTION_HANDLER": "common.exception_handlers.vinta_exception_handler",
    # Scoped (not project-wide) throttles. `payment-webhook` covers the
    # unauthenticated inbound provider webhook endpoints (payments/views.py) — a
    # generous per-IP rate since it only needs to bound abuse, not normal provider
    # retry volume. `billing-write` covers the authenticated money-moving billing
    # write actions (change-plan, add-on purchase/cancel in payments/billing_views.py)
    # — each drives a real provider round trip, so it is rate-limited rather than
    # left unbounded even behind auth. `payment-provider` covers the unauthenticated
    # provider-credentials read endpoint (Phase 3) — cheap, no outbound provider call,
    # so a higher ceiling than the webhook scope.
    "DEFAULT_THROTTLE_RATES": {
        "payment-webhook": "60/min",
        "billing-write": "30/min",
        "payment-provider": "120/min",
    },
}

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True


USE_TZ = True

# Redis is an optional dependency. When REDIS_URL is empty, Redis-backed
# features (rate limiting, django-defender, celery result backend/beat)
# degrade gracefully instead of crashing the app.
REDIS_URL = config("REDIS_URL", default="")

# Redis circuit breaker (see common.redis). After this many consecutive Redis
# errors the breaker opens and short-circuits Redis access for the reset window.
REDIS_CIRCUIT_BREAKER_FAILURE_THRESHOLD = config(
    "REDIS_CIRCUIT_BREAKER_FAILURE_THRESHOLD", cast=int, default=5
)
REDIS_CIRCUIT_BREAKER_RESET_TIMEOUT = config(
    "REDIS_CIRCUIT_BREAKER_RESET_TIMEOUT", cast=float, default=30.0
)

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

# Django-defender (requires Redis). Only enabled when REDIS_URL is configured;
# otherwise the app, its middleware, and its admin URLs are skipped entirely.
DEFENDER_ENABLED = bool(REDIS_URL)
if DEFENDER_ENABLED:
    INSTALLED_APPS.append("defender")
    MIDDLEWARE.insert(
        MIDDLEWARE.index("csp.middleware.CSPMiddleware") + 1,
        "defender.middleware.FailedLoginMiddleware",
    )
    DEFENDER_LOGIN_FAILURE_LIMIT = 3
    DEFENDER_COOLOFF_TIME = 300  # 5 minutes
    DEFENDER_LOCKOUT_TEMPLATE = "defender/lockout.html"
    DEFENDER_REDIS_URL = REDIS_URL

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=5),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,  # IMPORTANT
    "BLACKLIST_AFTER_ROTATION": True,  # IMPORTANT
    "UPDATE_LAST_LOGIN": True,
}

# Wildcard origins are incompatible with credentialed requests: the browser
# rejects `Access-Control-Allow-Origin: *` when credentials are sent. List
# explicit origins so the response echoes the request origin instead.
CORS_ALLOWED_ORIGINS = config(
    "CORS_ALLOWED_ORIGINS",
    cast=Csv(),
)
from corsheaders.defaults import default_headers


CORS_ALLOW_HEADERS = (
    *default_headers,
    "x-session-token",
    "x-email-verification-key",
    "x-password-reset-key",
    "x-organization-id",
)
CORS_ALLOW_CREDENTIALS = True
HEADLESS_ONLY = True
# The User model has no separate username column (email is the USERNAME_FIELD /
# login identifier). None disables allauth's username generation entirely.
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_LOGIN_METHODS = {"email", "phone"}
ACCOUNT_SIGNUP_FIELDS = [
    "email*",
    "phone*",
    "password1",
    "password2",
    "first_name*",
    "last_name*",
]
SOCIALACCOUNT_AUTO_SIGNUP = True
# allauth defaults this to False (since v65), which discards OAuth access/refresh
# tokens after login — no SocialToken row is created. The calendar integration
# needs the stored token (+ refresh_token) to call Google/Microsoft on the
# user's behalf, so persistence must be enabled.
SOCIALACCOUNT_STORE_TOKENS = True
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
# The frontend's origin -- the root of every URL above. staging.py/production.py
# override it with their real frontend origin; this default matches the local dev
# frontend port used throughout HEADLESS_FRONTEND_URLS above.
FRONTEND_BASE_URL = config("FRONTEND_BASE_URL", default="http://localhost:3000").rstrip("/")
# The signed-in app, relative to FRONTEND_BASE_URL. The origin's own root is the
# public landing page, NOT the app, so a caller needing a stable
# non-organization-specific destination for a just-authenticated user (see
# accounts.post_auth_destination, which lands them here when their organization has
# no configured post-authentication redirect) must carry this path. A plain
# constant rather than an env var: it is a frontend route, identical in every
# environment, and the origin it hangs off is the part that varies. Joined at use
# time, never precomputed here -- staging.py/production.py reassign
# FRONTEND_BASE_URL after this module is imported, so a derived constant would
# silently keep the local dev origin there.
FRONTEND_DASHBOARD_PATH = "/dashboard"
MFA_SUPPORTED_TYPES = ["totp", "recovery_codes"]
MFA_PASSKEY_LOGIN_ENABLED = False
HEADLESS_SERVE_SPECIFICATION = True
HEADLESS_CLIENTS = ("app", "browser")
HEADLESS_ADAPTER = "accounts.account_adapters.HeadlessAdapter"
HEADLESS_TOKEN_STRATEGY = "accounts.token_strategies.AccessAndRefreshTokenStrategy"  # noqa: S105
ACCESS_TOKEN_EXPIRY_MINUTES = config("ACCESS_TOKEN_EXPIRY_MINUTES", cast=int, default=15)
REFRESH_TOKEN_EXPIRY_DAYS = config("REFRESH_TOKEN_EXPIRY_DAYS", cast=int, default=30)
ACCOUNT_LOGIN_METHODS = {"phone", "email"}
# Rollout gate: default off while Twilio validates our messaging profile per
# environment. An operator flips this in the environment (Render dashboard /
# .env) once approved there — no code change required.
ACCOUNT_PHONE_VERIFICATION_ENABLED = config(
    "ACCOUNT_PHONE_VERIFICATION_ENABLED", cast=bool, default=False
)
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
        # `Subscription.pending_billing_interval` shares `BillingInterval`'s
        # choices with `Subscription.billing_interval` -- without this,
        # drf-spectacular creates a second, redundant enum name for the same
        # value set.
        "PendingBillingIntervalEnum": "payments.billing_constants.BillingInterval.choices",
        # `PaymentProviderSerializer.provider` (payments/serializers.py) is a plain
        # `ChoiceField`, not a model field, so it has no field name to inherit a
        # canonical enum name from the way `Payment.payment_provider` etc. do --
        # pin it to the same enum name those fields already resolve to.
        "PaymentProviderEnum": "payments.constants.PaymentProviders.choices",
        # `calendar_integration`'s model field literally named `provider` (a
        # different, unrelated choice set: internal/google/microsoft/apple/ics) is
        # the other contender for the auto-derived "ProviderEnum" name that
        # `PaymentProviderSerializer.provider` above would otherwise also compete
        # for -- pin it explicitly so neither side falls back to an unstable
        # hash-suffixed name (e.g. "Provider331Enum").
        "ProviderEnum": "calendar_integration.constants.CalendarProvider.choices",
        # `legal.models.PolicyDocumentType` owns the published schema component
        # `DocumentTypeEnum`. The new `BillingProfile.document_type` enum (Phase 2)
        # would otherwise contest this name on a hash basis, risking a renamed
        # collision (e.g., "PolicyDocumentTypeEnum"). Pin `legal`'s existing,
        # already-published name so the legal app's client contract is not broken.
        "DocumentTypeEnum": "legal.models.PolicyDocumentType.choices",
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


# Logos are small brand assets (a wordmark / mark, not a photo). 5 MB is well above
# any legitimate PNG/JPEG/WebP logo and well below a size that could meaningfully
# strain storage or the unauthenticated delivery route's bandwidth.
BRANDING_LOGO_MAX_SIZE_BYTES = 5 * 1024 * 1024
# SVG is deliberately excluded: it can carry script and would render on our own
# login page, making it a stored-XSS surface -- see the plan's "Logo limits"
# guiding decision.
BRANDING_LOGO_CONTENT_TYPES = ("image/png", "image/jpeg", "image/webp")


def _user_administers_branding_eligible_organization(user):
    """``auth`` callable for the ``branding_logos`` destination.

    Settings modules must not import app code at module scope -- the app
    registry isn't ready during settings load. This deferred import is only
    ever invoked by s3direct's signing view at request time, well after
    startup, mirroring the pattern ``tenancy.models.
    resolve_branding_for_display`` uses for ``di_core.containers.container``.
    """
    from tenancy.permissions import user_administers_branding_eligible_organization

    return user_administers_branding_eligible_organization(user)


S3DIRECT_DESTINATIONS = {
    "profile_pictures": {
        "key": generate_s3direct_file_name,
        "key_args": "uploads/profile_pictures",
        "auth": lambda u: u.is_authenticated,
        # The media bucket has Object Ownership set to BucketOwnerEnforced, so ACLs are
        # disabled and every canned ACL except this one is rejected. s3direct offers no
        # way to omit the header — an empty `acl` makes it fall back to `public-read` —
        # so this is the only value that survives. Objects stay private either way: the
        # bucket blocks public access and is readable only through the signed-URL
        # CloudFront distribution.
        "acl": "bucket-owner-full-control",
    },
    "branding_logos": {
        "key": generate_s3direct_file_name,
        "key_args": "uploads/branding_logos",
        # Tightened from bare `is_authenticated`: the signing surface is not open
        # to every logged-in user on the platform, only to an admin of some
        # branding-eligible organization -- see the plan's "Logo upload path"
        # guiding decision.
        "auth": _user_administers_branding_eligible_organization,
        # Same constraint as `profile_pictures` above: BucketOwnerEnforced rejects
        # every other canned ACL, and s3direct always sends one.
        "acl": "bucket-owner-full-control",
        "allowed": list(BRANDING_LOGO_CONTENT_TYPES),
        "content_length_range": [1, BRANDING_LOGO_MAX_SIZE_BYTES],
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
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/calendar.freebusy",
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/calendar.events.freebusy",
            "https://www.googleapis.com/auth/calendar.calendars",
            "https://www.googleapis.com/auth/calendar.app.created",
            "https://www.googleapis.com/auth/calendar.events.owned",
            "https://www.googleapis.com/auth/calendar.addons.execute",
            "https://www.googleapis.com/auth/calendar.addons.current.event.read",
            "https://www.googleapis.com/auth/calendar.addons.current.event.write",
            "https://www.googleapis.com/auth/calendar.acls",
        ],
        "AUTH_PARAMS": {
            # offline + consent so Google returns a refresh_token (stored as
            # SocialToken.token_secret). Access tokens expire in ~1h; without a
            # refresh_token the calendar integration breaks after the first hour.
            "access_type": "offline",
            "prompt": "consent",
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
# Shared secret used to verify MercadoPago's `x-signature` webhook header. Empty
# by default (matches MERCADOPAGO_ACCESS_TOKEN's dev-time fallback), but an empty
# secret makes every webhook signature check fail closed (see
# payments.services.mercadopago_signature.verify_mercadopago_signature) rather
# than skip verification.
MERCADOPAGO_WEBHOOK_SECRET = config("MERCADOPAGO_WEBHOOK_SECRET", default="")
# Browser-safe public key used to initialize MercadoPago's payment form. Not a
# secret — intentionally omitted from environment isolation. Served on unauthenticated
# endpoints in Phase 3.
MERCADOPAGO_PUBLIC_KEY = config("MERCADOPAGO_PUBLIC_KEY", default="")

# Secret API key used to authenticate outbound calls to Stripe. No organization is
# routed onto Stripe yet, so an empty default is safe in every environment until
# then.
STRIPE_SECRET_KEY = config("STRIPE_SECRET_KEY", default="")
# Shared secret used to verify Stripe's `Stripe-Signature` webhook header. Empty
# by default, matching MERCADOPAGO_WEBHOOK_SECRET's fail-closed convention (see
# payments.services.stripe_signature.verify_stripe_event) rather than skip
# verification.
STRIPE_WEBHOOK_SECRET = config("STRIPE_WEBHOOK_SECRET", default="")
# Browser-safe public key used to initialize Stripe's payment form. Not a secret —
# intentionally omitted from environment isolation. Served on unauthenticated
# endpoints in Phase 3.
STRIPE_PUBLISHABLE_KEY = config("STRIPE_PUBLISHABLE_KEY", default="")

# System-wide default payment provider. Resolves to the organization's pinned
# provider when set; otherwise, every new charge and subscription defaults to this.
# Value must match a member of payments.constants.PaymentProviders. Validated at
# import time so a typo fails the deploy rather than every checkout.
_payment_provider = config("DEFAULT_PAYMENT_PROVIDER", default="stripe")
if _payment_provider not in PAYMENT_PROVIDER_SLUGS:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        f"DEFAULT_PAYMENT_PROVIDER={_payment_provider!r} is not a valid payment provider. "
        f"Choose from: {', '.join(PAYMENT_PROVIDER_SLUGS)}"
    )
DEFAULT_PAYMENT_PROVIDER = _payment_provider
del _payment_provider

# How far a webhook's signed `ts` may drift from "now" before it is rejected as
# stale (see payments.services.mercadopago_signature.verify_mercadopago_signature).
# Default matches Stripe's own convention; the Stripe adapter reuses this same
# setting rather than defining its own tolerance window.
WEBHOOK_SIGNATURE_TOLERANCE_SECONDS = 300

# The global fallback grace window (days) between a failed recurring charge and
# an organization moving from GRACE to RESTRICTED, used whenever the
# subscription's BillingPlan.grace_period_days is NULL. A per-plan override can
# still tune it without a deploy; this setting is only the catalog-wide default.
# Tunable per environment with no code change.
BILLING_DEFAULT_GRACE_PERIOD_DAYS = config("BILLING_DEFAULT_GRACE_PERIOD_DAYS", default=7, cast=int)

SALT_KEY = config("SALT_KEY")

TWILIO_ACCOUNT_SID = config("TWILIO_ACCOUNT_SID")
# Prefer API Key auth (recommended). Legacy auth token kept for backward compat.
TWILIO_API_KEY_SID = config("TWILIO_API_KEY_SID", default="")
TWILIO_API_KEY_SECRET = config("TWILIO_API_KEY_SECRET", default="")
TWILIO_AUTH_TOKEN = config("TWILIO_AUTH_TOKEN", default="")
TWILIO_NUMBER = config("TWILIO_NUMBER")
TWILIO_DEFAULT_BROADCAST_NUMBERS: list[str] = config(
    "TWILIO_DEFAULT_BROADCAST_NUMBERS", default=[], cast=Csv()
)

BASE_URL_DOMAIN = config("BASE_URL_DOMAIN", "localhost:8000")
BASE_URL_PROTOCOL = config("BASE_URL_PROTOCOL", "http")
NOTIFICATION_DEFAULT_BASE_URL_DOMAIN = BASE_URL_DOMAIN
NOTIFICATION_DEFAULT_BASE_URL_PROTOCOL = BASE_URL_PROTOCOL
BASE_URL = f"{BASE_URL_PROTOCOL}://{BASE_URL_DOMAIN}"

PUBLIC_API_REDIS_URL = config("PUBLIC_API_REDIS_URL", default=REDIS_URL)
PUBLIC_API_REQUESTS_PER_SECOND_LIMIT = config("PUBLIC_API_REQUESTS_PER_SECOND_LIMIT", default=5)
PUBLIC_API_REQUESTS_PER_MINUTE_LIMIT = config("PUBLIC_API_REQUESTS_PER_MINUTE_LIMIT", default=100)
PUBLIC_API_REQUESTS_PER_HOUR_LIMIT = config("PUBLIC_API_REQUESTS_PER_HOUR_LIMIT", default=1000)
PUBLIC_API_RATE_LIMITER_KEY = config("PUBLIC_API_RATE_LIMITER_KEY", default="public_api")

GOOGLE_CLIENT_ID = config("GOOGLE_CLIENT_ID", default="")
GOOGLE_CLIENT_SECRET = config("GOOGLE_CLIENT_SECRET", default="")
