from django.conf import settings
from django.contrib import admin
from django.http import Http404
from django.urls import include, path
from django.views.decorators.csrf import csrf_exempt

from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from rest_framework.routers import DefaultRouter
from strawberry.django.views import GraphQLView
from vinta_billing.routing import get_extra_patterns as get_payments_extra_patterns
from vinta_billing.routing import get_routes as get_payments_routes

from calendar_integration.routes import routes as calendar_integration_routes
from legal.routes import routes as legal_routes
from notifications.routes import routes as notifications_routes
from organizations.routes import extra_patterns as organizations_extra_patterns
from organizations.routes import routes as organizations_routes
from organizations.views import AcceptInvitationView
from public_api.routes import routes as public_api_routes
from public_api.schema import schema
from users.routes import routes as users_routes
from webhooks.routes import routes as webhooks_routes


router = DefaultRouter(use_regex_path=False)

# Called, not imported as a module-level list: `vinta_billing.routing.get_routes()` /
# `get_extra_patterns()` build the route table fresh from the currently-configured
# `VINTA_BILLING['VIEW_MIXIN']` / `SERVICE_CONTAINER` each call (`apply_view_mixin`
# is itself idempotent -- see that function's docstring -- so calling this more than
# once, e.g. under a test that reloads this module, does not multiply the mixin).
payments_routes = get_payments_routes()
# `use_regex_path=False` above matches this project's router, so the two provider
# webhooks (which a router in either mode can only mis-render, see `get_extra_patterns`'s
# own docstring) need `trailing_slash` left at its default (`True`) to match every
# other route this router builds.
payments_extra_patterns = get_payments_extra_patterns()

routes = (
    *calendar_integration_routes,
    *legal_routes,
    *notifications_routes,
    *organizations_routes,
    *payments_routes,
    *public_api_routes,
    *users_routes,
    *webhooks_routes,
)
for route in routes:
    router.register(route["regex"], route["viewset"], basename=route["basename"])


def frontend_view(request, *args, **kwargs):
    raise Http404()


referenced_frontend_urlpatterns = [
    path("accept-invitation/<str:key>/", frontend_view, name="invitation"),
]


urlpatterns = [
    path("auth/", include("accounts.urls")),
    path("auth/", include("allauth.socialaccount.urls")),
    path("auth/", include("allauth.socialaccount.providers.google.urls")),
    path("auth/", include("allauth.headless.urls")),
    path("", include((router.urls, "api")), name="api"),
    path("", include(organizations_extra_patterns)),
    path("", include(payments_extra_patterns)),
    path("public/", include("calendar_integration.token_urls")),
    path("api/", include("calendar_integration.webhook_urls")),
    path(
        "invitations/accept",
        AcceptInvitationView.as_view(),
        name="accept-invitation",
    ),
    path("s3direct/", include("s3direct.urls")),
    path("super/", admin.site.urls, name="admin"),
    # drf-spectacular
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "schema/swagger-ui/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path(
        "schema/redoc/",
        SpectacularRedocView.as_view(url_name="schema"),
        name="redoc",
    ),
    path("graphql/", csrf_exempt(GraphQLView.as_view(schema=schema))),
    # *referenced_frontend_urlpatterns,
]

# django-defender admin URLs require Redis; only mount them when enabled.
if getattr(settings, "DEFENDER_ENABLED", False):
    urlpatterns.append(path("super/defender/", include("defender.urls")))
