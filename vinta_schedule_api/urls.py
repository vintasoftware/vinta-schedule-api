from django.contrib import admin
from django.urls import include, path

from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from rest_framework.routers import DefaultRouter

from calendar_integration.routes import routes as calendar_integration_routes
from payments.routes import routes as payments_routes
from users.routes import routes as users_routes


router = DefaultRouter(use_regex_path=False)

routes = (
    *calendar_integration_routes,
    *payments_routes,
    *users_routes,
)
for route in routes:
    router.register(route["regex"], route["viewset"], basename=route["basename"])

urlpatterns = [
    path("auth/", include("accounts.urls")),
    path("auth/", include("allauth.socialaccount.urls")),
    path("auth/", include("allauth.socialaccount.providers.google.urls")),
    path("auth/", include("allauth.headless.urls")),
    path("", include((router.urls, "api")), name="api"),
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
]
