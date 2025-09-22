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

from calendar_integration.routes import routes as calendar_integration_routes
from organizations.routes import routes as organizations_routes
from organizations.views import AcceptInvitationView
from payments.routes import routes as payments_routes
from public_api.schema import schema
from users.routes import routes as users_routes
from webhooks.routes import routes as webhooks_routes


router = DefaultRouter(use_regex_path=False)

routes = (
    *calendar_integration_routes,
    *organizations_routes,
    *payments_routes,
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
    path("public/", include("calendar_integration.token_urls")),
    path(
        "invitations/accept",
        AcceptInvitationView.as_view(),
        name="accept-invitation",
    ),
    path("s3direct/", include("s3direct.urls")),
    path("super/", admin.site.urls, name="admin"),
    path("super/defender/", include("defender.urls")),
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
