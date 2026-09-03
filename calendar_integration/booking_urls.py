"""URL patterns for the unauthenticated booking-code REST surface.

Mirrors ``token_urls.py`` / ``webhook_urls.py``: an ``app_name`` plus a
``DefaultRouter``. Mounted at ``public/booking/`` (see
``vinta_schedule_api/urls.py``). No `organization_id` in any path -- the
organization is always derived from the resolved code (or, for codeless
group booking, from the ``CalendarGroup``), never from client input.

Phase 0 registers no viewsets: the router is empty, so every sub-path 404s.
Phases 1-5 each register one action here.
"""

from django.urls import include, path

from rest_framework.routers import DefaultRouter


app_name = "calendar_booking_api"

router = DefaultRouter()

urlpatterns = [
    path("", include(router.urls)),
]
