"""URL patterns for the unauthenticated booking-code REST surface.

Mirrors ``token_urls.py`` / ``webhook_urls.py``: an ``app_name`` plus a
``DefaultRouter``. Mounted at ``public/booking/`` (see
``vinta_schedule_api/urls.py``). No `organization_id` in any path -- the
organization is always derived from the resolved code (or, for codeless
group booking, from the ``CalendarGroup``), never from client input.

Phase 0 registered no viewsets: the router was empty, so every sub-path
404d. Phase 1 registers the first one; Phases 2-5 each register one more.

Phase 3b (public group route): the group route addresses ``CalendarGroup``
by its opaque ``public_booking_slug`` -- never by the integer primary key.
Phase 3 shipped this route keyed by the integer id, which, combined with the
absence of ``organization_id`` from every path on this surface and the
decision to decline throttling, made the codeless branch a cross-tenant
enumeration oracle: an anonymous caller could walk ``group_id`` 1..N and
learn, from the 404/403/201 split, which groups exist in ANY organization
and which accept public scheduling. The capture group is named
``public_slug`` (not ``group_id``) throughout this surface -- ``booking_views``
reads ``kwargs["public_slug"]`` -- and matches the same character class
Django's built-in ``slug`` path converter uses (``[-a-zA-Z0-9_]+``), which is
exactly the URL-safe base64 alphabet ``secrets.token_urlsafe`` produces. The
router here is regex-based (``SimpleRouter``/``DefaultRouter`` embed the
``register()`` prefix directly into a ``re_path``), so the equivalent of
Django's ``path()`` ``<slug:public_slug>`` converter syntax is spelled as a
named regex group, matching this file's existing style for ``group_id``
before it.
"""

from django.urls import include, path

from rest_framework.routers import DefaultRouter

from calendar_integration.booking_views import (
    BookingCodeCalendarEventViewSet,
    BookingCodeGroupEventViewSet,
)


app_name = "calendar_booking_api"

router = DefaultRouter()
router.register(
    r"calendar-events",
    BookingCodeCalendarEventViewSet,
    basename="booking-calendar-events",
)
router.register(
    r"calendar-groups/(?P<public_slug>[-a-zA-Z0-9_]+)/events",
    BookingCodeGroupEventViewSet,
    basename="booking-calendar-group-events",
)

urlpatterns = [
    path("", include(router.urls)),
]
