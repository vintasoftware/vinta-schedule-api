"""ViewSet scaffolding for the unauthenticated booking-code REST surface.

``BookingCodeViewMixin`` is the base every ``public/booking/`` viewset
(Phases 1-5) builds on. It disables DRF's default authentication and
permission classes -- the booking code itself is the credential, not a
session/JWT/system-user token -- and DI-wires the four services those
viewsets need, so each phase's endpoint diff stays thin instead of
re-deriving the same plumbing six times (see Phase 0's goal in the plan).

Phase 0 adds no concrete viewset. ``booking_urls.py`` registers an empty
router until Phase 1. Phases 1-5 import the code-resolution / range-
validation / duration-pin / write-authorization / error-translation helpers
directly from ``calendar_integration.booking_auth``
(``resolve_booking_code_from_request``, ``resolve_booking_code_opaquely``,
``client_ip_from_request``, ``validate_code_gated_range``,
``pinned_duration_error``, ``resolve_and_authorize_write``,
``translate_booking_write_errors``) rather than through a wrapper on this
mixin -- this mixin owns only the shared authentication/permission posture
and DI wiring, not the helpers themselves.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Annotated

from django.db import transaction

from dependency_injector.wiring import Provide, inject
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet


if TYPE_CHECKING:
    # Stub-only type alias (``rest_framework-stubs/permissions.pyi``), not present
    # at runtime -- matches ``APIView.permission_classes``'s own declared type
    # exactly, so the mixin's ``permission_classes`` annotation below does not
    # conflict with it when a concrete viewset combines this mixin with a real
    # ``APIView`` subclass (e.g. ``GenericViewSet``).
    from rest_framework.permissions import _PermissionClass

from calendar_integration.booking_auth import (
    BOOKING_CODE_HEADER,
    booking_code_header,
    client_ip_from_request,
    pinned_duration_error,
    resolve_and_authorize_write,
    translate_booking_write_errors,
)
from calendar_integration.booking_exceptions import (
    NotPermittedAPIException,
    SlotUnavailableAPIException,
)
from calendar_integration.constants import EventManagementPermissions
from calendar_integration.exceptions import (
    CalendarGroupError,
    CalendarServiceNotInjectedError,
    PermissionServiceInitializationError,
)
from calendar_integration.models import CalendarEvent, CalendarGroup, CalendarManagementToken
from calendar_integration.serializers import (
    BookingCodeEventCreateSerializer,
    BookingCodeGroupEventCreateSerializer,
    CalendarEventSerializer,
)
from calendar_integration.services.bookable_slots_service import BookableSlotsService
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    CalendarEventInputData,
    CalendarGroupEventInputData,
    CalendarGroupSlotSelectionInputData,
    EventExternalAttendanceInputData,
    ExternalAttendeeInputData,
)


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

    # Typed to match ``APIView``'s own stub declarations (``Sequence[type[
    # BaseAuthentication]]`` / ``Sequence[_PermissionClass]``) -- this mixin is not
    # itself an ``APIView`` subclass, so without an explicit, compatible annotation
    # mypy flags the eventual `class Concrete(BookingCodeViewMixin, GenericViewSet)`
    # combination as two unrelated bases defining the same attribute incompatibly.
    authentication_classes: Sequence[type[BaseAuthentication]] = ()
    permission_classes: "Sequence[_PermissionClass]" = ()

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


@extend_schema(tags=["Booking Codes"])
class BookingCodeCalendarEventViewSet(BookingCodeViewMixin, GenericViewSet):
    """Unauthenticated, code-gated single-calendar booking.

    ``POST /public/booking/calendar-events/`` books an event on the calendar bound
    to the presented ``X-Booking-Code``. Ports
    ``calendar_integration.mutations.create_calendar_event_with_code`` to REST --
    see that mutation's docstring for the full five-step flow this mirrors.
    """

    serializer_class = BookingCodeEventCreateSerializer

    @extend_schema(
        request=BookingCodeEventCreateSerializer,
        responses={201: CalendarEventSerializer},
        parameters=[
            OpenApiParameter(
                name=BOOKING_CODE_HEADER,
                type=str,
                location=OpenApiParameter.HEADER,
                required=True,
                description="Single-use booking code, minted with the CREATE "
                "permission and scoped to a single calendar (not a group).",
            ),
        ],
        summary="Book an event on a single calendar with a booking code",
    )
    def create(self, request: Request, *args, **kwargs) -> Response:
        """Resolve the code, then create the event and consume the code atomically.

        Create FIRST, then consume, matching the GraphQL original
        (``create_calendar_event_with_code``). Both statements run inside the
        same outer ``transaction.atomic()`` block (see step 5 below), so any
        exception either one raises -- including ``consume_code``'s
        ``TokenAlreadyUsedError`` on a lost race -- unwinds the whole
        transaction: the DB outcome (one event, code consumed once) is the
        same regardless of which statement runs first. What create-first
        actually buys is that both racers do the provider-side write (the
        adapter's ``create_event`` call) before either one's outcome is
        decided, rather than the loser being turned away by the row lock
        before ever reaching the provider.
        """
        permission_service = self.calendar_permission_service
        calendar_service = self.calendar_service
        if permission_service is None or calendar_service is None:
            raise CalendarServiceNotInjectedError(
                "calendar_permission_service / calendar_service not configured; "
                "check the DI container."
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # --- Step 1: resolve the code, check permission, and resolve org ---
        token, code, org = resolve_and_authorize_write(
            request, permission_service, EventManagementPermissions.CREATE
        )

        # --- Step 2: scope check -- must be single-calendar (not group) ---
        if token.calendar is None:
            raise NotPermittedAPIException("This code is not scoped to a single calendar.")

        # --- Step 3: extract client IP for audit ---
        source_ip = client_ip_from_request(request)

        # --- Step 4: build event data ---
        external_attendee = data["external_attendee"]
        event_data = CalendarEventInputData(
            title=data["title"],
            description=data.get("description", ""),
            start_time=data["start_time"],
            end_time=data["end_time"],
            timezone=data["timezone"],
            external_attendances=[
                EventExternalAttendanceInputData(
                    external_attendee=ExternalAttendeeInputData(
                        email=external_attendee["email"],
                        name=external_attendee.get("name", ""),
                    )
                )
            ],
        )

        # --- Step 5: atomic create + consume ---
        # Create FIRST, then consume, matching the GraphQL original. Both statements
        # share this one outer atomic() block, so the DB outcome is the same either
        # order -- see the docstring above for what create-first actually changes
        # (both racers reach the provider adapter, instead of the loser being turned
        # away by consume_code's row lock first). ``translate_booking_write_errors``
        # maps the exception vocabulary shared with the group-booking viewset onto
        # the booking-code API exceptions.
        with translate_booking_write_errors(
            permission_denied_message="This code does not permit booking on this calendar."
        ):
            with transaction.atomic():
                calendar_service.initialize_without_provider(user_or_token=code, organization=org)
                event = calendar_service.create_event(token.calendar.id, event_data)
                permission_service.consume_code(token, source_ip)
        # create_event raises OverLimitError at the organization's postpaid
        # event_occurrences allowance (no payment method on file). Unlike the domain
        # errors above, this is not a booking-code-specific outcome the patient can
        # retry around, so it is left to propagate to the shared vinta_exception_handler,
        # which renders the shared 402 over-limit contract -- it must NOT be caught
        # into this vocabulary.

        context = self.get_serializer_context()
        optimized_event = (
            CalendarEventSerializer(context=context)
            .get_optimized_queryset(CalendarEvent.objects.filter_by_organization(org.id))
            .get(id=event.id)
        )
        return Response(
            CalendarEventSerializer(optimized_event, context=context).data,
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=["Booking Codes"])
class BookingCodeGroupEventViewSet(BookingCodeViewMixin, GenericViewSet):
    """Unauthenticated group booking, code-gated or codeless.

    ``POST /public/booking/calendar-groups/<group_id>/events/`` books a grouped
    event through the ``CalendarGroup`` named by the path. Ports both
    ``calendar_integration.mutations.create_calendar_group_event_with_code`` (when
    ``X-Booking-Code`` is present) and the codeless
    ``calendar_integration.mutations.create_calendar_group_event`` (when it is
    absent) to REST -- see those mutations' docstrings for the flows this mirrors.

    **Coded** (header present): ``group_id`` comes STRICTLY from the resolved
    token, never from the path or the request body. The path segment is a
    routing convenience only, checked against ``token.calendar_group`` and
    rejected (``403 NOT_PERMITTED``, never a ``404``) on any mismatch -- a
    ``404`` would confirm the code's real group to someone probing a different
    id, which is an enumeration oracle this endpoint must not offer.

    **Codeless** (header absent): no code is resolved or consumed -- there is
    none. ``group_id`` comes from the path, exactly as the client sent it: it
    is the client's own input here, not a secret, so a ``404`` for a
    nonexistent id discloses nothing the client did not already know -- the
    mirror image of the coded rule above. Authorization is delegated entirely
    to ``CalendarGroupService.create_grouped_event``, which allows the booking
    only when the group's own ``accepts_public_scheduling`` is ``True``
    (``403 NOT_PERMITTED`` otherwise). The group's pinned duration, if any,
    still applies on this branch: the pin lives on the ``CalendarGroup``, not
    on the code, so a codeless booking is constrained by it exactly like a
    coded one.

    The coded path always wins when the header is present -- a group that
    accepts public scheduling but is handed a valid group code still books
    through the code and consumes it.
    """

    serializer_class = BookingCodeGroupEventCreateSerializer

    @extend_schema(
        request=BookingCodeGroupEventCreateSerializer,
        responses={201: CalendarEventSerializer},
        parameters=[
            OpenApiParameter(
                name=BOOKING_CODE_HEADER,
                type=str,
                location=OpenApiParameter.HEADER,
                required=False,
                description="Single-use booking code, minted with the CREATE "
                "permission and scoped to a calendar group (not a single calendar). "
                "OPTIONAL on this endpoint only: when omitted, the booking is "
                "authorized instead by the path group's own "
                "accepts_public_scheduling flag (codeless public group booking, "
                "GraphQL parity with createCalendarGroupEvent). When present, the "
                "coded path always wins -- a public group handed a valid group "
                "code still books through it and consumes it.",
            ),
        ],
        summary="Book a grouped event through a calendar group, with or without a booking code",
    )
    def create(self, request: Request, *args, **kwargs) -> Response:
        """Create the grouped event, resolving and consuming a code only when one is presented.

        **Coded**: create FIRST, then consume, matching the GraphQL original
        (``create_calendar_group_event_with_code``) and Phase 1's single-calendar
        endpoint. Both statements run inside the same outer ``transaction.atomic()``
        block below, so any exception either one raises -- including
        ``consume_code``'s ``TokenAlreadyUsedError`` on a lost race -- unwinds the
        whole transaction: the DB outcome (one event, code consumed once) is the
        same regardless of which statement runs first. What create-first actually
        buys is that both racers do the provider-side write (the adapter's
        ``create_event`` call) before either one's outcome is decided, rather than
        the loser being turned away by the row lock before ever reaching the
        provider.

        **Codeless**: no code exists to create-then-consume around, so only the
        create runs. Organization is derived from the path group itself (there is
        no token to read it from), and the group-level
        ``accepts_public_scheduling`` gate inside ``create_grouped_event`` is the
        entire authorization decision -- it is not restated here.
        """
        permission_service = self.calendar_permission_service
        calendar_service = self.calendar_service
        calendar_group_service = self.calendar_group_service
        if permission_service is None or calendar_service is None or calendar_group_service is None:
            raise CalendarServiceNotInjectedError(
                "calendar_permission_service / calendar_service / calendar_group_service "
                "not configured; check the DI container."
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        path_group_id = int(kwargs["group_id"])
        code = booking_code_header(request)
        token: CalendarManagementToken | None = None

        if code is None:
            # --- Codeless branch: skip code resolution entirely. The group id
            # is the client's OWN input here (not a secret the way a coded
            # token's group is), so a 404 for a nonexistent one discloses
            # nothing the client did not already know. Do NOT "fix" this to a
            # 403 to mirror the coded branch's mismatch check below -- the two
            # differ on purpose; see that check's comment.
            try:
                # unscoped(): the organization is not yet known -- this lookup
                # is what determines it, so filter_by_organization cannot run
                # yet. This is the codeless counterpart of resolving the
                # organization from the token's organization_id on the coded
                # branch.
                group = (
                    CalendarGroup.objects.unscoped()
                    .select_related("organization")
                    .get(id=path_group_id)
                )
            except CalendarGroup.DoesNotExist as exc:
                raise NotFound("Calendar group not found.") from exc
            org = group.organization
        else:
            # --- resolve the code, check permission, and resolve org ---
            token, code, org = resolve_and_authorize_write(
                request, permission_service, EventManagementPermissions.CREATE
            )

            # --- scope check -- must be group-scoped (not single-calendar) ---
            if token.calendar_group is None:
                raise NotPermittedAPIException(
                    "This code is not scoped to a calendar group. "
                    "Use the single-calendar booking endpoint for calendar-scoped codes."
                )

            # --- path/token scope assertion -- the path <group_id> is a routing
            # convenience only. A mismatch is 403 NOT_PERMITTED, never a 404: a
            # 404 would confirm the code's real group to a caller probing a
            # different id. This is the mirror image of the codeless branch
            # above, where the path <group_id> IS the client's own input and a
            # 404 is the correct, non-disclosing response for a nonexistent
            # one -- do NOT "fix" one of these two to match the other.
            if path_group_id != token.calendar_group_fk_id:
                raise NotPermittedAPIException("This code is not scoped to this calendar group.")

            group = token.calendar_group

        # --- pinned-duration message, ahead of dispatch. The guarantee itself
        # is enforced independently inside can_perform_group_scheduling -- this
        # exists purely so the response names the pinned duration instead of
        # the generic permission-denied message. Applies on BOTH branches: the
        # pin lives on the CalendarGroup, not on the code, so a codeless
        # booking is constrained by it exactly like a coded one -- a codeless
        # booking presents no credential to carry a pin, but the group's own
        # duration is not a property of the code. ---
        duration_error = pinned_duration_error(group, data["start_time"], data["end_time"])
        if duration_error is not None:
            raise duration_error

        # --- build group event data -- group_id comes from the token when one
        # was resolved (to enforce scope), otherwise from the path (the
        # codeless branch has no token to read it from). ---
        external_attendee = data["external_attendee"]
        group_event_data = CalendarGroupEventInputData(
            group_id=token.calendar_group.id if token is not None else path_group_id,
            title=data["title"],
            description=data.get("description", ""),
            start_time=data["start_time"],
            end_time=data["end_time"],
            timezone=data["timezone"],
            slot_selections=[
                CalendarGroupSlotSelectionInputData(
                    slot_id=s["slot_id"],
                    calendar_ids=list(s["calendar_ids"]),
                )
                for s in data["slot_selections"]
            ],
            external_attendances=[
                EventExternalAttendanceInputData(
                    external_attendee=ExternalAttendeeInputData(
                        email=external_attendee["email"],
                        name=external_attendee.get("name", ""),
                    )
                )
            ],
        )

        permission_denied_message = (
            "This code does not permit booking on this calendar group."
            if token is not None
            else (
                "This group does not accept public scheduling. "
                "A token or scheduling code is required."
            )
        )

        # ``calendar_group_service`` is the DI container's own Factory-provided
        # instance, wired with its own (uninitialized) ``CalendarService`` and
        # ``CalendarPermissionService``. Explicitly wire in the code-initialized
        # ``calendar_service`` (and its ``calendar_permission_service``, which
        # carries the resolved token, when there is one) so the primary-calendar
        # create and the group-level ``can_perform_group_scheduling`` gate both
        # see the same auth context -- without this the group service would
        # authorize against an uninitialized permission service and deny every
        # private-group booking. On the codeless branch ``code`` stays ``None``,
        # matching the GraphQL codeless mutation's own
        # ``initialize_without_provider(organization=organization)`` call (no
        # ``user_or_token``). ``translate_booking_write_errors`` maps the
        # exception vocabulary shared with the single-calendar viewset onto the
        # booking-code API exceptions; ``CalendarGroup.DoesNotExist``,
        # ``CalendarGroupError``, and ``PermissionServiceInitializationError``
        # are group-only and stay mapped here.
        try:
            with translate_booking_write_errors(
                permission_denied_message=permission_denied_message
            ):
                with transaction.atomic():
                    calendar_service.initialize_without_provider(
                        user_or_token=code, organization=org
                    )
                    calendar_group_service.calendar_service = calendar_service
                    calendar_group_service.calendar_permission_service = (
                        calendar_service.calendar_permission_service
                    )
                    calendar_group_service.initialize(organization=org)
                    event = calendar_group_service.create_grouped_event(group_event_data)
                    # No code to consume on the codeless branch -- there is none.
                    if token is not None:
                        permission_service.consume_code(token, client_ip_from_request(request))
        except PermissionServiceInitializationError as exc:
            # Defensive: in practice the codeless group-level gate denies via a
            # plain ``PermissionDenied`` (already mapped above), matching the
            # GraphQL codeless mutation's own dominant failure mode. This stays
            # mapped to the identical message for parity with GraphQL's own
            # defensive ``except PermissionServiceInitializationError`` branch,
            # in case a future change to the gate's internals raises this
            # instead.
            raise NotPermittedAPIException(
                "This group does not accept public scheduling. "
                "A token or scheduling code is required."
            ) from exc
        except CalendarGroup.DoesNotExist as exc:
            if token is None:
                # Codeless: group_id is the client's own path input, not a
                # secret -- see the branch comment above. Do not map this to
                # 403 to mirror the coded branch below.
                raise NotFound("Calendar group not found.") from exc
            # Coded: per the path/token scope rule above, never disclose
            # whether a group id exists via a bare 404. This is effectively
            # unreachable in practice -- the token's calendar_group FK is ON
            # DELETE CASCADE, so a deleted group takes the token with it and
            # code resolution above would already have failed as
            # INVALID_CODE -- but the mapping stays defensive rather than
            # leaking existence if that invariant ever changes.
            raise NotPermittedAPIException(
                "This code is not scoped to a valid calendar group."
            ) from exc
        except CalendarGroupError as exc:
            # Slot taken / invalid selection -- nothing consumed (txn rolled
            # back), patient may retry with a different slot.
            raise SlotUnavailableAPIException() from exc
        # create_grouped_event raises OverLimitError at the organization's postpaid
        # event_occurrences allowance (no payment method on file). Unlike the domain
        # errors above, this is not a booking-code-specific outcome the patient can
        # retry around, so it is left to propagate to the shared vinta_exception_handler,
        # which renders the shared 402 over-limit contract -- it must NOT be caught
        # into this vocabulary.

        context = self.get_serializer_context()
        optimized_event = (
            CalendarEventSerializer(context=context)
            .get_optimized_queryset(CalendarEvent.objects.filter_by_organization(org.id))
            .get(id=event.id)
        )
        return Response(
            CalendarEventSerializer(optimized_event, context=context).data,
            status=status.HTTP_201_CREATED,
        )
