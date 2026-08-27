import os
import re
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

from cuid2 import cuid_wrapper
from decouple import Csv, config  # type: ignore
from dj_database_url import parse as db_url
from vinta_billing.provider_slugs import MERCADOPAGO, PAYMENT_PROVIDER_SLUGS, STRIPE


BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def base_dir_join(*args):
    return os.path.join(BASE_DIR, *args)


SITE_ID = 1

DEBUG = True

ADMINS = ["hugo@vinta.com.br"]

AUTH_USER_MODEL = "users.User"

# --- vinta_audit_logs ---------------------------------------------------
# The audit log app is generic and knows nothing about this project. These four
# settings are the whole of what it needs to learn.
#
# The two model settings are the AUTH_USER_MODEL pattern: they name the concrete
# scope and identity `audit_integration` defines, so audit records are scoped to
# an Organization and actors can be memberships, API tokens and single-use codes
# rather than only users.
AUDIT_SCOPE_MODEL = "audit_integration.OrganizationAuditScope"
AUDIT_IDENTITY_MODEL = "audit_integration.OrganizationAuditIdentity"
# The two factory settings are dotted paths rather than DI providers, because a
# package should not force its DI library on the projects installing it. Both
# resolve through this project's container -- see audit_integration.services.
AUDIT_CELERY_APP = "vinta_schedule_api.celery.app"
AUDIT_SERVICE_FACTORY = "audit_integration.services.audit_service_factory"
AUDIT_REPOSITORY_FACTORY = "audit_integration.services.audit_repository_factory"

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
    "audit_integration",
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
    # ``vinta-django-orgs``. Deliberately NOT in INTERNAL_INSTALLED_APPS: that list
    # drives di_core's DI wiring and names only this project's own apps. Its Django
    # app label is ``vinta_orgs`` (the package renamed itself in 0.2.0 precisely so
    # it would not collide with a project app called ``organizations``), so our
    # ``organizations`` app keeps its label and every one of its tables keeps its
    # name -- see organizations/tests/test_app_identity.py, which is the regression
    # gate for that. Listed *before* INTERNAL_INSTALLED_APPS so ``admin.autodiscover``
    # reaches the package's admin module first; organizations/admin.py additionally
    # imports it explicitly so the unregistration below does not depend on this order.
    "vinta_orgs.apps.OrganizationsConfig",
    # ``vinta-django-billing`` -- the billing engine of record. Like ``vinta_orgs``,
    # deliberately NOT in INTERNAL_INSTALLED_APPS: that list drives di_core's DI
    # wiring and names only this project's own apps. The ``payments`` app keeps its
    # label and stays installed, but owns configuration (the resource registry and
    # the four other seams, the plan catalog, the Celery entry points) rather than
    # models -- every billing table lives under the ``vinta_billing`` label from
    # ``payments/migrations/0024_move_billing_to_vinta_billing.py`` onward.
    "vinta_billing",
    # ``vinta-django-audit-logs`` -- the audit log itself. Same reasoning as the two
    # packages above: INTERNAL_INSTALLED_APPS drives di_core's DI wiring and names
    # only this project's own apps. There is a concrete cost to getting this wrong
    # here, not just an inconsistency -- wiring a package makes the container import
    # every module in it at startup, and ``vinta_audit_logs.tasks`` is the one module
    # that pulls in Celery. The package uses no ``@inject`` anywhere, so it would be
    # paying that import for nothing.
    #
    # ``audit_integration`` stays in the list above, and has to: its
    # ``audit_service_factory`` / ``audit_repository_factory`` are ``@inject``-ed,
    # and an unwired module means the markers are never resolved.
    "vinta_audit_logs",
    *INTERNAL_INSTALLED_APPS,
]

# ``vinta-django-orgs`` reads these two as *top-level* settings (Django resolves
# ``Meta.swappable`` with a plain ``getattr(settings, ...)``), which is why they sit
# outside SHARED_SCHEMA_ORGANIZATIONS below. Pointing them at our models marks the
# package's own concrete ``Organization`` / ``OrganizationMembership`` as swapped
# out: no tables are created for them, and ``User.delete()`` does not carry a
# phantom CASCADE to a second, unused membership table.
ORGANIZATION_MODEL = "organizations.Organization"
ORGANIZATION_MEMBERSHIP_MODEL = "organizations.OrganizationMembership"

SHARED_SCHEMA_ORGANIZATIONS = {
    # The one retriever we use. ``retrieve_by_domain`` (subdomain tenancy),
    # ``retrieve_by_http_header`` (``Organization-Slug``) and ``retrieve_by_session``
    # are all deliberately omitted: this API resolves the organization only from
    # the ``X-Organization-Id`` header, never by subdomain, a differently-named
    # header, or session state.
    "ORGANIZATION_RETRIEVERS": [
        "common.org_retrievers.retrieve_by_x_organization_id",
    ],
    # We have no catch-all organization. Left at the package default (``'default'``)
    # this would make ``SingleOrganizationModelMixin.save()`` run a
    # ``WHERE slug = 'default'`` lookup for every scoped row saved without an
    # explicit organization -- and, worse, silently adopt a real organization that
    # happened to claim that slug. ``organizations.slug_validation`` reserves the
    # word, so no organization can; ``None`` makes the intent explicit and skips the
    # query entirely.
    "DEFAULT_ORGANIZATION_SLUG": None,
    # The package's middleware is not installed (``TenantScopedViewMixin`` owns
    # organization resolution, because its rules are membership-aware and it runs
    # after DRF authentication), so nothing would write this -- stated so a future
    # reader does not have to check.
    "ADD_ORGANIZATION_TO_SESSION": False,
    # An organization-scoped query that runs with nothing bound raises
    # ``OrganizationNotFoundError`` rather than quietly returning no rows. Enabled
    # together with the first models to scope implicitly: an
    # empty result is indistinguishable from "no data yet" in a task or a
    # management command, where a missing binding is the likelier explanation,
    # and this is the whole safety argument for migrating without a feature flag.
    #
    # Reaching outside the bound organization stays available and stays explicit:
    # ``filter_by_organization(...)`` / ``exclude_by_organization(...)`` (which
    # start from the unscoped queryset) and ``original_manager``.
    "STRICT_ORGANIZATION_FILTER": True,
    # Points ``vinta_orgs.testing.reseed_organization_groups()`` (wired in via
    # ``pytest_plugins = ["vinta_orgs.testing"]`` in the root ``conftest.py``) at
    # *our* three seeded groups instead of the package's own
    # ``organization_owner`` default. Reads the **live** catalog
    # (``organizations.permission_catalog.GROUP_PERMISSIONS``), not
    # ``organizations/migrations/0028_seed_permission_groups.py``'s frozen
    # literals -- the migration is the production path and is deliberately
    # allowed to drift from the code as it evolves; the seeder must not be.
    # Without this a ``transaction=True`` test flushes ``auth_group`` /
    # ``auth_group_permissions`` (data migrations are not replayed by ``flush``)
    # and every membership built by a later test in that worker's session
    # silently holds no permission at all.
    "ORGANIZATION_GROUP_SEEDERS": [
        "organizations.permission_catalog.seed_organization_groups",
    ],
}

# model-bakery resolves a generator by the field's *exact* class, so it does not
# inherit one from a custom field's base. Without an entry here, ``baker.make()`` on
# any model carrying the field raises ``TypeError: ... is not supported by baker``.
# Both sides are dotted paths, so model-bakery (a dev dependency) is never imported
# here and this stays inert outside tests.
BAKER_CUSTOM_FIELDS_GEN = {
    "common.fields.NaiveDateTimeField": "common.testing.baker_generators.gen_naive_datetime",
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

# Spelled out for the first time here. It was previously left at Django's implicit
# default, which is exactly the first entry -- so this list *adds* the second
# backend and changes nothing else.
#
# ``ModelBackend`` is kept rather than replaced even though
# ``OrganizationModelBackend`` subclasses it and would answer every
# authentication call identically: a live session records the dotted path of the
# backend that authenticated it (``_auth_user_backend``), and
# ``django.contrib.auth.get_user`` logs the session out when that path is no
# longer in this list. Dropping the default entry would therefore sign out every
# existing session on deploy for no gain.
#
# What the second backend adds is *permissions*, not authentication:
# ``OrganizationModelBackend.get_all_permissions`` unions the user's global
# permissions with the ones their ``OrganizationMembership`` in the
# **currently-bound** organization carries (``vinta_orgs.state``'s contextvar,
# which ``TenantScopedViewMixin`` / ``PublicApiSystemUserMiddleware`` bind for the
# duration of a request). With nothing bound the organization half is empty, so an
# unbound path answers exactly what ``ModelBackend`` alone answered.
#
# The two backends share cache attribute names on the user object
# (``_perm_cache``, ``_user_perm_cache``, ``_group_perm_cache``) -- deliberate and
# safe, because both fill them with the same *global* permission set;
# organization permissions live in separate, organization-keyed caches
# (``_organization_*_perm_cache``) that the stock backend never touches.
#
# Registered **unsubclassed**, as the package ships it -- not a repo-owned
# subclass. Under ``0.2.0`` a deactivated membership still resolved its group
# permissions (``_get_membership`` did not filter ``is_active``), which would
# have forced a repo-owned subclass to close before any caller could safely
# read ``has_perm``. ``0.3.0`` fixes that at the source: ``is_active`` now lives on
# ``AbstractOrganizationMembership`` and is filtered *inside*
# ``OrganizationModelBackend._get_membership``, so a deactivated administrator
# resolves exactly what a non-member resolves -- nothing. See
# ``organizations/tests/test_permission_backend.py`` for the pinned regression.
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "vinta_orgs.auth_backends.OrganizationModelBackend",
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
    # provider-credentials read endpoint — cheap, no outbound provider call,
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
# Annotated because `Csv()` is untyped (decouple ships no stubs), so mypy cannot infer
# the element type and asks for one.
CORS_ALLOWED_ORIGINS: list[str] = config(
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
        "PendingBillingIntervalEnum": "vinta_billing.constants.BillingInterval.choices",
        # `PaymentProviderSerializer.provider` (payments/serializers.py) is a plain
        # `ChoiceField`, not a model field, so it has no field name to inherit a
        # canonical enum name from the way `Payment.payment_provider` etc. do --
        # pin it to the same enum name those fields already resolve to.
        "PaymentProviderEnum": "vinta_billing.constants.PaymentProviders.choices",
        # `calendar_integration`'s model field literally named `provider` (a
        # different, unrelated choice set: internal/google/microsoft/apple/ics) is
        # the other contender for the auto-derived "ProviderEnum" name that
        # `PaymentProviderSerializer.provider` above would otherwise also compete
        # for -- pin it explicitly so neither side falls back to an unstable
        # hash-suffixed name (e.g. "Provider331Enum").
        "ProviderEnum": "calendar_integration.constants.CalendarProvider.choices",
        # `legal.models.PolicyDocumentType` owns the published schema component
        # `DocumentTypeEnum`. The new `BillingProfile.document_type` enum
        # would otherwise contest this name on a hash basis, risking a renamed
        # collision (e.g., "PolicyDocumentTypeEnum"). Pin `legal`'s existing,
        # already-published name so the legal app's client contract is not broken.
        "DocumentTypeEnum": "legal.models.PolicyDocumentType.choices",
        # `SystemUserScopeSerializer.value` (public_api/serializers.py) is a plain
        # `ChoiceField` over the same choice set the `available_resources` fields
        # already publish as `AvailableResourcesEnum`. Enum names are derived from
        # the *field* name, so without this the scope catalog would mint a second,
        # redundant `ValueEnum` for the identical value set -- and contest that
        # generic name with any other `value` field that ever gains choices. Pin
        # the already-published name so both sides of the round trip (the catalog
        # a client reads, the list it posts back) are one type.
        "AvailableResourcesEnum": "public_api.constants.PublicAPIResources.choices",
    },
}


def is_valid_url(s):
    if not s:
        return False

    val = URLValidator()
    try:
        val(s)
    except ValidationError:
        return False

    return True


def append_uuid_to_filename(filename):
    cuid_generator: Callable[[], str] = cuid_wrapper()

    filename_without_ext, ext = os.path.splitext(filename)
    return f"{filename_without_ext}_{cuid_generator()}{ext}"


def generate_s3direct_file_name(original_file_name, dest):
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
# login page, making it a stored-XSS surface.
BRANDING_LOGO_CONTENT_TYPES = ("image/png", "image/jpeg", "image/webp")


def _user_administers_branding_eligible_organization(user):
    """``auth`` callable for the ``branding_logos`` destination.

    Settings modules must not import app code at module scope -- the app
    registry isn't ready during settings load. This deferred import is only
    ever invoked by s3direct's signing view at request time, well after
    startup, mirroring the pattern ``organizations.models.
    resolve_branding_for_display`` uses for ``di_core.containers.container``.
    """
    from organizations.permissions import user_administers_branding_eligible_organization

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
        # branding-eligible organization.
        "auth": _user_administers_branding_eligible_organization,
        # Same constraint as `profile_pictures` above: BucketOwnerEnforced rejects
        # every other canned ACL, and s3direct always sends one.
        "acl": "bucket-owner-full-control",
        "allowed": list(BRANDING_LOGO_CONTENT_TYPES),
        "content_length_range": [1, BRANDING_LOGO_MAX_SIZE_BYTES],
    },
}

# `dict[str, Any]`, not an inferred type: allauth's per-provider config has no single
# shape -- "apple" carries only APPS, "google" adds SCOPE (list[str]) and AUTH_PARAMS
# (dict[str, str]). Left bare, mypy infers the value type from whichever provider block
# is assigned first and rejects every other one.
SOCIALACCOUNT_PROVIDERS: dict[str, Any] = {}
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
            }
        ]
    }
if config("FACEBOOK_APP_ID", default=""):
    SOCIALACCOUNT_PROVIDERS["facebook"] = {
        "APPS": [
            {
                "client_id": config("FACEBOOK_APP_ID", default=""),
                "secret": config("FACEBOOK_APP_SECRET", default=""),
            }
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
            },
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
# vinta_billing.services.mercadopago_signature.verify_mercadopago_signature) rather
# than skip verification.
MERCADOPAGO_WEBHOOK_SECRET = config("MERCADOPAGO_WEBHOOK_SECRET", default="")
# Browser-safe public key used to initialize MercadoPago's payment form. Not a
# secret — intentionally omitted from environment isolation. Served on unauthenticated
# endpoints.
MERCADOPAGO_PUBLIC_KEY = config("MERCADOPAGO_PUBLIC_KEY", default="")

# Secret API key used to authenticate outbound calls to Stripe. No organization is
# routed onto Stripe yet, so an empty default is safe in every environment until
# then.
STRIPE_SECRET_KEY = config("STRIPE_SECRET_KEY", default="")
# Shared secret used to verify Stripe's `Stripe-Signature` webhook header. Empty
# by default, matching MERCADOPAGO_WEBHOOK_SECRET's fail-closed convention (see
# vinta_billing.services.stripe_signature.verify_stripe_event) rather than skip
# verification.
STRIPE_WEBHOOK_SECRET = config("STRIPE_WEBHOOK_SECRET", default="")
# Browser-safe public key used to initialize Stripe's payment form. Not a secret —
# intentionally omitted from environment isolation. Served on unauthenticated
# endpoints.
STRIPE_PUBLISHABLE_KEY = config("STRIPE_PUBLISHABLE_KEY", default="")

# System-wide default payment provider. Resolves to the organization's pinned
# provider when set; otherwise, every new charge and subscription defaults to this.
# Value must match a member of vinta_billing.constants.PaymentProviders. Validated at
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
# stale (see vinta_billing.services.mercadopago_signature.verify_mercadopago_signature).
# Default matches Stripe's own convention; the Stripe adapter reuses this same
# setting rather than defining its own tolerance window.
WEBHOOK_SIGNATURE_TOLERANCE_SECONDS = 300

# The global fallback grace window (days) between a failed recurring charge and
# an organization moving from GRACE to RESTRICTED, used whenever the
# subscription's BillingPlan.grace_period_days is NULL. A per-plan override can
# still tune it without a deploy; this setting is only the catalog-wide default.
# Tunable per environment with no code change.
BILLING_DEFAULT_GRACE_PERIOD_DAYS = config("BILLING_DEFAULT_GRACE_PERIOD_DAYS", default=7, cast=int)

# The host's configuration of `vinta_billing` -- the engine's five seams and
# the handful of scalars it cannot infer on its own. `vinta_billing.conf`
# rejects any key outside its own defaults at first access, so a typo here
# fails loudly rather than silently keeping the library's default.
#
# `vinta_billing` is not in INSTALLED_APPS yet (that is Phase 1), so nothing
# reads this dict today -- it exists now because every later phase reads one
# of these five objects out of settings, and Phase 0's whole job is to make
# them resolvable.
VINTA_BILLING = {
    # `organizations.Organization` is self-referential (`parent`) with a
    # `can_invite_organizations` reseller flag -- exactly the shape
    # `ParentFieldHierarchy` expects. See `payments.seams.hierarchy
    # .ResellerHierarchy`, which only names the two field names.
    "HIERARCHY": "payments.seams.hierarchy.ResellerHierarchy",
    # `organizations.0028_seed_permission_groups` already seeds
    # `organization_admin` and `organization_billing_owner` with
    # `manage_billing`, so the stricter predicate is safe to select on day
    # one here -- see AGENTS.md's billing section / the migration plan's
    # "Who may manage billing" guiding decision for why the package does not
    # default to this itself.
    "BILLING_MANAGER_PREDICATE": "vinta_billing.permissions.member_holding_manage_billing",
    # Forwards dunning/usage-warning notifications to the vintasend
    # `NotificationService` the DI container already builds for every other
    # notification-sending service in this project.
    "NOTIFIER": "payments.seams.notifier.NotificationServiceNotifier",
    # What "a billable occurrence happened" means here: one `CalendarEvent`
    # start, in or out of a recurring series.
    "OCCURRENCE_SOURCE": "payments.seams.occurrences.CalendarEventOccurrenceSource",
    # The only postpaid resource this project registers (see
    # `payments.seams.resources`) -- set explicitly rather than left for the
    # "single registered postpaid resource" fallback, so a second postpaid
    # resource registered later fails loudly instead of silently changing
    # which one the meter bills.
    "METERED_RESOURCE_KEY": "event_occurrences",
    # The counterpart to `BILLING_MANAGER_PREDICATE`: who the dunning ladder
    # and usage warnings tell. Same "safe because 0028 already seeds the
    # grant" reasoning.
    "BILLING_RECIPIENTS": "vinta_billing.recipients.members_holding_manage_billing",
    # `vinta_billing`'s MercadoPago adapters `reverse()` their own webhook
    # callback URLs through this namespace (`vinta_billing/urls_helpers.py`),
    # and those two names -- `Payments-payment-update` and
    # `Payments-subscription-payment-update` -- are the *only* thing this key
    # governs.
    #
    # Empty, not "api". Up to `vinta-django-billing` 0.3.0 both webhooks came
    # out of the shared DRF router, which this project mounts as
    # `include((router.urls, "api"))` (`vinta_schedule_api/urls.py`), so "api"
    # was right. 0.4.0 moved them into `routing.get_extra_patterns()` -- each
    # carries the provider slug as a URL segment, which a router can only spell
    # in its own mode -- and this project mounts those patterns *unnamespaced*
    # (`path("", include(payments_extra_patterns))`), exactly as it already did
    # for the two `billing/payment-provider/` endpoints. Left at "api", or at
    # the package's own default ("billing"), MercadoPago callback-URL
    # construction raises `NoReverseMatch` -- and only once MercadoPago is
    # actually exercised, so a green suite would not catch a wrong value here
    # on its own. `payments/tests/seams/test_settings.py` reverses both names
    # through `namespaced()` rather than pinning this literal.
    "URL_NAMESPACE": "",
    "SITE_DOMAIN": SITE_DOMAIN,
    "DEFAULT_CURRENCY": "USD",
    "GRACE_PERIOD_DAYS": BILLING_DEFAULT_GRACE_PERIOD_DAYS,
    # Matches `usage_warning_service.APPROACHING_LIMIT_THRESHOLD`, the value
    # this project already enforces today.
    "USAGE_WARNING_THRESHOLD": 0.8,
    # Mixed in front of every tenant-scoped viewset `vinta_billing.routing
    # .get_routes()` / `get_extra_patterns()` mount, so `X-Organization-Id`
    # resolution (this project's own, not the package's) applies to them too.
    "VIEW_MIXIN": "common.utils.view_utils.TenantScopedViewMixin",
    # Where the shipped views and the admin build their services from.
    # Resolved lazily per view construction (`vinta_billing.services.container
    # .get_service_container`), so `di_container.<provider>.override(...)` in
    # tests is honoured -- see `payments/admin.py` and the deleted
    # `payments/views.py` / `payments/billing_views.py`, whose whole reason
    # for existing was building services this setting now lets the package's
    # own views and admin do instead.
    "SERVICE_CONTAINER": "di_core.containers.container",
    # Same env vars as ever -- `STRIPE_SECRET_KEY` / `MERCADOPAGO_ACCESS_TOKEN`
    # / friends, defined above. Render env groups and CI are untouched; only
    # the shape they are assembled into changed.
    "PROVIDERS": {
        STRIPE: {
            "API_KEY": STRIPE_SECRET_KEY,
            "WEBHOOK_SECRET": STRIPE_WEBHOOK_SECRET,
            "PUBLISHABLE_KEY": STRIPE_PUBLISHABLE_KEY,
        },
        MERCADOPAGO: {
            "ACCESS_TOKEN": MERCADOPAGO_ACCESS_TOKEN,
            "WEBHOOK_SECRET": MERCADOPAGO_WEBHOOK_SECRET,
            "PUBLIC_KEY": MERCADOPAGO_PUBLIC_KEY,
        },
    },
    "DEFAULT_PROVIDER": DEFAULT_PAYMENT_PROVIDER,
}

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
