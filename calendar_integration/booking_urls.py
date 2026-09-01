"""URL patterns for the unauthenticated booking-code REST surface.

Mirrors ``token_urls.py`` / ``webhook_urls.py``: an ``app_name`` plus a
``DefaultRouter``. Mounted at ``public/booking/`` (see
``vinta_schedule_api/urls.py``). No `organization_id` in any path -- the
organization is always derived from the resolved code (or, for codeless
group booking, from the ``CalendarGroup``), never from client input.

Phase 0 registered no viewsets: the router was empty, so every sub-path
404d. Phase 1 registers the first one; Phases 2-5 each register one more.
"""

from django.urls import include, path

from rest_framework.routers import DefaultRouter

from calendar_integration.booking_views import BookingCodeCalendarEventViewSet


app_name = "calendar_booking_api"

router = DefaultRouter()
router.register(
    r"calendar-events",
    BookingCodeCalendarEventViewSet,
    basename="booking-calendar-events",
)

urlpatterns = [
    path("", include(router.urls)),
]
