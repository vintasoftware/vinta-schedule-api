"""
URL patterns for token-based calendar event management.
"""

from django.urls import include, path

from rest_framework.routers import DefaultRouter

from calendar_integration.token_views import TokenCalendarEventViewSet


app_name = "calendar_token_api"

router = DefaultRouter()
router.register(
    r"organizations/(?P<organization_id>\d+)/events",
    TokenCalendarEventViewSet,
    basename="token-events",
)

urlpatterns = [
    path("", include(router.urls)),
]
