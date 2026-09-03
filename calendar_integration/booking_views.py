"""ViewSet scaffolding for the unauthenticated booking-code REST surface.

``BookingCodeViewMixin`` is the base every ``public/booking/`` viewset
(Phases 1-5) builds on. It disables DRF's default authentication and
permission classes -- the booking code itself is the credential, not a
session/JWT/system-user token -- and DI-wires the four services those
viewsets need, so each phase's endpoint diff stays thin instead of
re-deriving the same plumbing six times (see Phase 0's goal in the plan).

Phase 0 adds no concrete viewset. ``booking_urls.py`` registers an empty
router until Phase 1. Phases 1-5 import the code-resolution / range-
validation / duration-pin helpers directly from ``calendar_integration.
booking_auth`` (``resolve_booking_code_from_request``,
``resolve_booking_code_opaquely``, ``client_ip_from_request``,
``validate_code_gated_range``, ``pinned_duration_error``) rather than
through a wrapper on this mixin -- this mixin owns only the shared
authentication/permission posture and DI wiring, not the helpers
themselves.
"""

from typing import Annotated

from dependency_injector.wiring import Provide, inject

from calendar_integration.services.bookable_slots_service import BookableSlotsService
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.calendar_service import CalendarService


class BookingCodeViewMixin:
    """Base mixin for unauthenticated, code-gated booking endpoints.

    Every concrete viewset under ``public/booking/`` inherits from this
    (alongside the DRF viewset/mixin bases it needs) rather than the
    authenticated ``*VintaScheduleModelViewSet`` bases: the credential is a
    single-use booking code, not a session/JWT, so tenant binding comes from
    the resolved token's organization, not from ``TenantScopedViewMixin``.

    This mixin owns the authentication/permission posture and DI wiring
    only. It does not wrap ``calendar_integration.booking_auth``'s helpers
    -- call them directly (e.g. ``booking_auth.resolve_booking_code_from_request(
    request, self.calendar_permission_service)``) from each concrete
    viewset action.
    """

    calendar_permission_service: "CalendarPermissionService | None"
    calendar_service: "CalendarService | None"
    calendar_group_service: "CalendarGroupService | None"
    bookable_slots_service: "BookableSlotsService | None"

    authentication_classes = ()
    permission_classes = ()

    @inject
    def __init__(
        self,
        calendar_permission_service: Annotated[
            "CalendarPermissionService | None", Provide["calendar_permission_service"]
        ] = None,
        calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
        calendar_group_service: Annotated[
            "CalendarGroupService | None", Provide["calendar_group_service"]
        ] = None,
        bookable_slots_service: Annotated[
            "BookableSlotsService | None", Provide["bookable_slots_service"]
        ] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.calendar_permission_service = calendar_permission_service
        self.calendar_service = calendar_service
        self.calendar_group_service = calendar_group_service
        self.bookable_slots_service = bookable_slots_service
