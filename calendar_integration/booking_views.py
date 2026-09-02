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
    InvalidCodeAPIException,
    NotPermittedAPIException,
    SlotUnavailableAPIException,
)
from calendar_integration.constants import EventManagementPermissions
from calendar_integration.exceptions import (
    CalendarGroupError,
    CalendarGroupValidationError,
    CalendarServiceNotInjectedError,
    InvalidTokenError,
)
from calendar_integration.models import CalendarEvent, CalendarGroup, CalendarManagementToken
from calendar_integration.serializers import (
    BookingCodeEventCreateSerializer,
    BookingCodeGroupEventCreateSerializer,
    BookingCodeRescheduleSerializer,
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
    EventAttendanceInputData,
    EventExternalAttendanceInputData,
    ExternalAttendeeInputData,
    ResourceAllocationInputData,
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

    ``POST /public/booking/calendar-groups/<public_slug>/events/`` books a
    grouped event through the ``CalendarGroup`` named by the path. Ports both
    ``calendar_integration.mutations.create_calendar_group_event_with_code`` (when
    ``X-Booking-Code`` is present) and the codeless
    ``calendar_integration.mutations.create_calendar_group_event`` (when it is
    absent) to REST -- see those mutations' docstrings for the flows this mirrors.

    The path segment addresses ``CalendarGroup.public_booking_slug`` -- an
    opaque, unguessable, globally-unique identifier (Phase 3b) -- never the
    integer primary key. Phase 3 shipped this route keyed by the integer id,
    which, with no ``organization_id`` anywhere in this surface's paths and
    throttling declined, made the codeless branch a cross-tenant enumeration
    oracle: an anonymous caller could walk ``group_id`` 1..N and learn, from
    the 404/403/201 split, which groups exist in ANY organization and which
    accept public scheduling. The slug alone authorizes nothing -- every
    group has one, public or private -- ``accepts_public_scheduling`` still
    gates codeless booking exactly as before.

    **Coded** (header present): the addressed group comes STRICTLY from the
    resolved token, never from the path or the request body. The path
    segment is a routing convenience only, compared against the TOKEN'S OWN
    ``calendar_group.public_booking_slug`` (never resolved by looking a group
    up BY the path's slug -- see the ordering note on that comparison below)
    and rejected (``403 NOT_PERMITTED``, never a ``404``) on any mismatch -- a
    ``404`` would confirm the code's real group to someone probing a
    different slug, which is an enumeration oracle this endpoint must not
    offer, exactly as it must not for the integer id Phase 3 replaced.

    **Codeless** (header absent): no code is resolved or consumed -- there is
    none. The group comes from the path's slug, exactly as the client sent
    it: it is the client's own input here, not a secret (unlike the coded
    branch's token-bound group), so a ``404`` for a slug that resolves to no
    group discloses nothing the client did not already know -- the mirror
    image of the coded rule above, and now safe precisely because the
    identifier is unguessable rather than sequential. Authorization is
    delegated entirely to ``CalendarGroupService.create_grouped_event``,
    which allows the booking only when the group's own
    ``accepts_public_scheduling`` is ``True`` (``403 NOT_PERMITTED``
    otherwise). The group's pinned duration, if any, still applies on this
    branch: the pin lives on the ``CalendarGroup``, not on the code, so a
    codeless booking is constrained by it exactly like a coded one.

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

        path_public_slug = kwargs["public_slug"]
        code = booking_code_header(request)
        token: CalendarManagementToken | None = None

        if code is None:
            # --- Codeless branch: skip code resolution entirely. The slug is
            # the client's OWN input here (not a secret the way a coded
            # token's group is), so a 404 for a slug that resolves to no
            # group discloses nothing the client did not already know -- and
            # is now SAFE to disclose, because the slug is unguessable
            # (Phase 3b) rather than a sequential integer someone could walk.
            # Do NOT "fix" this to a 403 to mirror the coded branch's
            # mismatch check below -- the two differ on purpose; see that
            # check's comment.
            try:
                # unscoped(): the organization is not yet known -- this lookup
                # is what determines it, so filter_by_organization cannot run
                # yet. This is the codeless counterpart of resolving the
                # organization from the token's organization_id on the coded
                # branch.
                group = (
                    CalendarGroup.objects.unscoped()
                    .select_related("organization")
                    .get(public_booking_slug=path_public_slug)
                )
            except CalendarGroup.DoesNotExist as exc:
                raise NotFound("Calendar group not found.") from exc
            org = group.organization
            resolved_group_id = group.id
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

            # --- path/token scope assertion -- the path <public_slug> is a
            # routing convenience only. Compare it against the TOKEN'S OWN
            # resolved group's slug (``token.calendar_group`` was already
            # fetched by the scope check above, keyed strictly by the
            # token's own ``calendar_group_fk_id`` -- never by the path
            # value) rather than resolving a group BY the path's slug and
            # comparing ids. That ordering matters: a lookup keyed on the
            # untrusted path slug would perform a distinguishable "does a
            # group with this slug exist" query, reintroducing exactly the
            # enumeration oracle this phase closes, even if the eventual
            # HTTP status stayed 403. Comparing two already-known strings in
            # memory (the token's own slug vs. the path's) means the coded
            # branch never queries the DB by the path's slug at all -- a
            # mismatch is 403 NOT_PERMITTED, never a 404: a 404 would confirm
            # the code's real group to a caller probing a different slug.
            # This is the mirror image of the codeless branch above, where
            # the path <public_slug> IS the client's own input and a 404 is
            # the correct, non-disclosing response for one that resolves to
            # no group -- do NOT "fix" one of these two to match the other.
            if path_public_slug != token.calendar_group.public_booking_slug:
                raise NotPermittedAPIException("This code is not scoped to this calendar group.")
            resolved_group_id = token.calendar_group.id

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

        # --- build group event data -- resolved_group_id comes from the
        # token's own resolved group when one was resolved (to enforce
        # scope), otherwise from the group already resolved by slug on the
        # codeless branch above (the codeless branch has no token to read it
        # from, and the path carries only the slug, never the integer id). ---
        external_attendee = data["external_attendee"]
        group_event_data = CalendarGroupEventInputData(
            group_id=resolved_group_id,
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
        # booking-code API exceptions, including a private group's denial on the
        # codeless branch: ``can_perform_group_scheduling`` returns ``False`` via
        # its ``token is None`` short-circuit, ``create_grouped_event`` raises a
        # plain ``django.core.exceptions.PermissionDenied``, and that context
        # manager's own ``except DjangoPermissionDenied`` maps it to
        # ``NotPermittedAPIException`` above -- do not add an
        # ``except PermissionServiceInitializationError`` here for that case,
        # that exception is only raised by ``has_permission``, which this path
        # never reaches (``accepts_public_scheduling`` and the group-scope check
        # both return before it, and ``create_event`` skips its own permission
        # check via ``group_authorized=True``). ``CalendarGroup.DoesNotExist``
        # and ``CalendarGroupError`` are group-only and stay mapped here.
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
        except CalendarGroup.DoesNotExist as exc:
            if token is None:
                # Codeless: public_slug is the client's own path input, not a
                # secret -- see the branch comment above. Do not map this to
                # 403 to mirror the coded branch below.
                raise NotFound("Calendar group not found.") from exc
            # Coded: per the path/token scope rule above, never disclose
            # whether a group with this slug exists via a bare 404. This is
            # effectively unreachable in practice -- the token's
            # calendar_group FK is ON DELETE CASCADE, so a deleted group
            # takes the token with it and code resolution above would
            # already have failed as INVALID_CODE -- but the mapping stays
            # defensive rather than leaking existence if that invariant ever
            # changes.
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


@extend_schema(tags=["Booking Codes"])
class BookingCodeRescheduleEventViewSet(BookingCodeViewMixin, GenericViewSet):
    """Unauthenticated, code-gated single-calendar reschedule.

    ``POST /public/booking/events/reschedule/`` moves the event bound to the
    presented ``X-Booking-Code`` to a new time. Ports
    ``calendar_integration.mutations.reschedule_calendar_event_with_code`` to
    REST -- see that mutation's docstring for the full flow this mirrors.

    ``calendar_id`` and ``event_id`` come STRICTLY from the resolved token,
    never from the request. Only ``start_time`` / ``end_time`` / ``timezone``
    change: title, description, internal attendances, external attendances
    (including the ``ExternalAttendee`` id, so ``serialize_event_data_input``
    correlates status correctly), and resource allocations are snapshotted
    from the existing event and replayed UNCHANGED, so that
    ``_determine_required_update_permissions`` yields exactly ``{RESCHEDULE}``
    -- a naive partial update would demand permissions this code does not
    carry and fail with a misleading ``NOT_PERMITTED``.
    """

    serializer_class = BookingCodeRescheduleSerializer

    @extend_schema(
        request=BookingCodeRescheduleSerializer,
        responses={201: CalendarEventSerializer},
        parameters=[
            OpenApiParameter(
                name=BOOKING_CODE_HEADER,
                type=str,
                location=OpenApiParameter.HEADER,
                required=True,
                description="Single-use booking code, minted with the RESCHEDULE "
                "permission and bound to a specific event on a single calendar "
                "(not a group).",
            ),
        ],
        summary="Reschedule an event on a single calendar with a booking code",
    )
    def create(self, request: Request, *args, **kwargs) -> Response:
        """Snapshot the bound event's preserved details, then update + consume atomically.

        Update FIRST, then consume, matching the GraphQL original and
        ``BookingCodeCalendarEventViewSet.create``'s create-then-consume ordering.
        Both statements share the one outer ``transaction.atomic()`` block below,
        so the DB outcome (one update, code consumed once) is the same regardless
        of which runs first -- see that method's docstring for what update-first
        actually changes (both racers reach the write adapter rather than the
        loser being turned away by ``consume_code``'s row lock first).
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

        # --- resolve the code, check permission, and resolve org ---
        token, code, org = resolve_and_authorize_write(
            request,
            permission_service,
            EventManagementPermissions.RESCHEDULE,
            permission_denied_message="This code does not permit rescheduling.",
        )

        # --- scope check -- must be bound to a specific event, and single-calendar
        # (not group) -- a group-scoped code routes to the group reschedule endpoint. ---
        if token.event is None:
            raise NotPermittedAPIException("This code is not bound to a specific event.")
        if token.calendar_group is not None:
            raise NotPermittedAPIException(
                "This code is scoped to a calendar group. Use the group reschedule "
                "endpoint for group-scoped codes."
            )

        # --- pinned-duration message, ahead of dispatch. A reschedule code's pin
        # constrains the NEW span, not the event's current one -- refusing a move
        # to a different span even when the event's current span already matches
        # it. The guarantee itself is enforced independently inside
        # can_perform_update; this exists purely so the response names the
        # pinned duration. ---
        duration_error = pinned_duration_error(token, data["start_time"], data["end_time"])
        if duration_error is not None:
            raise duration_error

        source_ip = client_ip_from_request(request)

        # calendar_id and event_id come strictly from the token -- not from client
        # input -- so the code can only ever affect the exact event it was minted for.
        event_id: int = token.event_fk_id  # type: ignore[assignment]
        calendar_id: int = token.event.calendar_fk_id  # type: ignore[assignment]

        # --- load the existing event to snapshot its current details (title,
        # description, attendances, external_attendances, resource_allocations). ---
        try:
            existing_event = (
                CalendarEvent.objects.filter_by_organization(org.id)
                .select_related("calendar")
                .prefetch_related("attendances", "resource_allocations")
                .get(id=event_id, calendar_fk_id=calendar_id)
            )
        except CalendarEvent.DoesNotExist as exc:
            raise InvalidCodeAPIException() from exc

        # --- build the preserved-details event data: copy every preserved field
        # and override ONLY the time fields, so that
        # _determine_required_update_permissions yields exactly {RESCHEDULE}. ---
        preserved_attendances = [
            EventAttendanceInputData(user_id=attendance.membership_user_id)
            for attendance in existing_event.attendances.all()
            if attendance.membership_user_id is not None
        ]
        # Include the ExternalAttendee id so that serialize_event_data_input can
        # correlate the status correctly and produce a CalendarEventData matching
        # the old event's external_attendees by email, ensuring
        # _check_attendances_update_necessary_permissions sees no change.
        preserved_external_attendances = [
            EventExternalAttendanceInputData(
                external_attendee=ExternalAttendeeInputData(
                    email=ea.external_attendee_fk.email,  # type: ignore[union-attr]
                    name=ea.external_attendee_fk.name or "",  # type: ignore[union-attr]
                    id=ea.external_attendee_fk_id,  # type: ignore[union-attr]
                )
            )
            for ea in existing_event.external_attendances.select_related("external_attendee")
        ]
        # Skip any allocation with a null calendar_fk_id, mirroring the recurring-
        # event transfer guard in calendar_event_service.py.
        preserved_resource_allocations = [
            ResourceAllocationInputData(resource_id=ra.calendar_fk_id)  # type: ignore[arg-type]
            for ra in existing_event.resource_allocations.all()
            if ra.calendar_fk_id
        ]

        event_data = CalendarEventInputData(
            title=existing_event.title,
            description=existing_event.description or "",
            start_time=data["start_time"],
            end_time=data["end_time"],
            timezone=data["timezone"],
            attendances=preserved_attendances,
            external_attendances=preserved_external_attendances,
            resource_allocations=preserved_resource_allocations,
        )

        # --- availability pre-check (code-path only), BEFORE the atomic block, so
        # the code is never consumed on an out-of-window attempt. ---
        if existing_event.calendar.manage_available_windows:
            calendar_service.initialize_without_provider(organization=org)
            available_windows = calendar_service.get_availability_windows_in_range(
                existing_event.calendar, data["start_time"], data["end_time"]
            )
            if not available_windows:
                raise SlotUnavailableAPIException()

        # --- atomic update + consume ---
        with translate_booking_write_errors(
            permission_denied_message="This code does not permit rescheduling this event."
        ):
            with transaction.atomic():
                calendar_service.initialize_without_provider(user_or_token=code, organization=org)
                event = calendar_service.update_event(calendar_id, event_id, event_data)
                permission_service.consume_code(token, source_ip)
        # update_event does not create a new event and therefore does not reach
        # the create-event postpaid metering guard -- there is no OverLimitError
        # for this endpoint to leave unswallowed.

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
class BookingCodeRescheduleGroupEventViewSet(BookingCodeViewMixin, GenericViewSet):
    """Unauthenticated, code-gated group reschedule.

    ``POST /public/booking/group-events/reschedule/`` moves the grouped event
    bound to the presented ``X-Booking-Code`` to a new time. Ports
    ``calendar_integration.mutations.reschedule_calendar_group_event_with_code``
    to REST -- see that mutation's docstring for the full flow this mirrors.

    ``event_id`` comes STRICTLY from the resolved token, never from the
    request. Only the times change -- title, description, attendances,
    resource allocations, and the group's calendar selections are preserved
    by ``CalendarGroupService.reschedule_grouped_event`` (time-only v1; full
    slot re-selection is deferred, per the plan's Non-goals).
    """

    serializer_class = BookingCodeRescheduleSerializer

    @extend_schema(
        request=BookingCodeRescheduleSerializer,
        responses={201: CalendarEventSerializer},
        parameters=[
            OpenApiParameter(
                name=BOOKING_CODE_HEADER,
                type=str,
                location=OpenApiParameter.HEADER,
                required=True,
                description="Single-use booking code, minted with the RESCHEDULE "
                "permission and bound to a specific event on a calendar group "
                "(not a single calendar).",
            ),
        ],
        summary="Reschedule a grouped event through a calendar group with a booking code",
    )
    def create(self, request: Request, *args, **kwargs) -> Response:
        """Reschedule the bound grouped event's times, then consume the code atomically.

        Update FIRST, then consume -- see
        ``BookingCodeRescheduleEventViewSet.create``'s docstring for what that
        ordering does and does not guarantee.
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

        # --- resolve the code, check permission, and resolve org ---
        token, code, org = resolve_and_authorize_write(
            request,
            permission_service,
            EventManagementPermissions.RESCHEDULE,
            permission_denied_message="This code does not permit rescheduling.",
        )

        # --- scope check -- must be bound to a specific event, and group-scoped
        # (not single-calendar) -- a single-calendar code routes to the other
        # reschedule endpoint. ---
        if token.event is None:
            raise NotPermittedAPIException("This code is not bound to a specific event.")
        if token.calendar_group is None:
            raise NotPermittedAPIException(
                "This code is not scoped to a calendar group. Use the single-calendar "
                "reschedule endpoint for calendar-scoped codes."
            )

        # --- pinned-duration message, ahead of dispatch -- see
        # BookingCodeRescheduleEventViewSet.create's comment on the same check;
        # the pin constrains the NEW span here too. ---
        duration_error = pinned_duration_error(token, data["start_time"], data["end_time"])
        if duration_error is not None:
            raise duration_error

        source_ip = client_ip_from_request(request)

        # event_id comes strictly from the token, never from client input, so the
        # code can only ever affect the exact grouped event it was minted for.
        event_id: int = token.event_fk_id  # type: ignore[assignment]

        # --- availability pre-check (code-path only) against the bound event's
        # primary calendar, BEFORE the atomic block, so the code is never consumed
        # on an out-of-window attempt. ---
        try:
            bound_event = (
                CalendarEvent.objects.filter_by_organization(org.id)
                .select_related("calendar")
                .get(id=event_id)
            )
        except CalendarEvent.DoesNotExist as exc:
            raise InvalidCodeAPIException() from exc

        primary_calendar = bound_event.calendar
        if primary_calendar is not None and primary_calendar.manage_available_windows:
            calendar_service.initialize_without_provider(organization=org)
            available_windows = calendar_service.get_availability_windows_in_range(
                primary_calendar, data["start_time"], data["end_time"]
            )
            if not available_windows:
                raise SlotUnavailableAPIException()

        # --- atomic reschedule + consume. calendar_group_service is wired with the
        # code-initialized calendar_service so both the primary-calendar update
        # and the group-level authorization checks see the same auth context. ---
        try:
            with translate_booking_write_errors(
                permission_denied_message="This code does not permit rescheduling this event."
            ):
                with transaction.atomic():
                    calendar_service.initialize_without_provider(
                        user_or_token=code, organization=org
                    )
                    calendar_group_service.calendar_service = calendar_service
                    calendar_group_service.initialize(organization=org)
                    event = calendar_group_service.reschedule_grouped_event(
                        event_id=event_id,
                        start_time=data["start_time"],
                        end_time=data["end_time"],
                        tz=data["timezone"],
                    )
                    permission_service.consume_code(token, source_ip)
        except CalendarGroupError as exc:
            # Slot outside a group-scoped availability window / quota rule, or an
            # invalid selection -- nothing consumed (txn rolled back), patient may
            # retry with a different slot. ``translate_booking_write_errors`` does
            # not map this hierarchy (it is group-only); mapped here instead,
            # matching ``BookingCodeGroupEventViewSet.create``'s own extra catch.
            raise SlotUnavailableAPIException() from exc

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
class BookingCodeCancelEventViewSet(BookingCodeViewMixin, GenericViewSet):
    """Unauthenticated, code-gated cancellation -- single-calendar or group.

    ``POST /public/booking/events/cancel/`` deletes the event bound to the
    presented ``X-Booking-Code``. Ports
    ``calendar_integration.mutations.cancel_event_with_code`` to REST -- see
    that mutation's docstring for the full flow this mirrors, including why it
    handles BOTH a calendar-bound and a group-bound cancel code via the SAME
    endpoint, dispatching on whether ``token.calendar_group`` is set.

    ``event_id`` comes STRICTLY from the resolved token, never from the
    request. No body is accepted. Returns ``204 No Content`` on success.
    """

    @extend_schema(
        request=None,
        responses={204: None},
        parameters=[
            OpenApiParameter(
                name=BOOKING_CODE_HEADER,
                type=str,
                location=OpenApiParameter.HEADER,
                required=True,
                description="Single-use booking code, minted with the CANCEL "
                "permission and bound to a specific event.",
            ),
        ],
        summary="Cancel an event, single-calendar or group-bound, with a booking code",
    )
    def create(self, request: Request, *args, **kwargs) -> Response:
        """Consume FIRST, then delete, matching the GraphQL original.

        The event FK on the token has ``on_delete=CASCADE``, so deleting the
        event would cascade-delete the token -- making a post-delete consume
        impossible. Consuming first (via ``SELECT FOR UPDATE``) keeps the row
        alive long enough to lock it; the cascade then removes the
        already-consumed token row when the event is deleted. Both statements
        share the one outer ``transaction.atomic()`` block below, so if the
        delete step raises unexpectedly, the whole block -- including the
        consume -- rolls back, leaving the code available for retry.
        """
        permission_service = self.calendar_permission_service
        calendar_service = self.calendar_service
        calendar_group_service = self.calendar_group_service
        if permission_service is None or calendar_service is None or calendar_group_service is None:
            raise CalendarServiceNotInjectedError(
                "calendar_permission_service / calendar_service / calendar_group_service "
                "not configured; check the DI container."
            )

        # --- resolve the code, check permission, and resolve org ---
        token, code, org = resolve_and_authorize_write(
            request,
            permission_service,
            EventManagementPermissions.CANCEL,
            permission_denied_message="This code does not permit cancellation.",
        )

        # --- scope check -- must be bound to a specific event. Dispatch below on
        # whether the token is group-scoped -- both the single-calendar and the
        # group path are handled by this same endpoint, exactly as the GraphQL
        # original does. ---
        if token.event is None:
            raise NotPermittedAPIException("This code is not bound to a specific event.")

        source_ip = client_ip_from_request(request)

        # event_id and (single-calendar path only) calendar_id come strictly from
        # the token, never from client input.
        event_id: int = token.event_fk_id  # type: ignore[assignment]
        single_calendar_id: int | None = (
            None if token.calendar_group is not None else token.event.calendar_fk_id
        )

        try:
            with translate_booking_write_errors(
                permission_denied_message="This code does not permit cancellation of this event."
            ):
                with transaction.atomic():
                    permission_service.consume_code(token, source_ip)
                    calendar_service.initialize_without_provider(
                        user_or_token=code, organization=org
                    )
                    if token.calendar_group is not None:
                        calendar_group_service.calendar_service = calendar_service
                        calendar_group_service.initialize(organization=org)
                        calendar_group_service.cancel_grouped_event(
                            event_id=event_id, delete_series=False
                        )
                    else:
                        calendar_service.delete_event(
                            calendar_id=single_calendar_id,  # type: ignore[arg-type]
                            event_id=event_id,
                            delete_series=False,
                        )
        except InvalidTokenError as exc:
            # consume_code re-fetched under SELECT FOR UPDATE and found no row
            # (e.g. the token was deleted between resolve and the lock). This is
            # NOT a genuine authorization failure -- surface it as INVALID_CODE.
            raise InvalidCodeAPIException() from exc
        except CalendarEvent.DoesNotExist as exc:
            # The event was concurrently deleted between resolve and the delete call.
            raise InvalidCodeAPIException() from exc
        except CalendarGroupValidationError as exc:
            # Group path: the bound event is not actually a grouped event (scope
            # mismatch), or a cancel_grouped_event precondition failed for a
            # structural reason -- a permission/scope issue, not a slot-availability
            # one. Caught ahead of the broader CalendarGroupError below.
            raise NotPermittedAPIException(
                "This code does not permit cancellation of this event."
            ) from exc
        except CalendarGroupError as exc:
            raise SlotUnavailableAPIException("The event could not be cancelled.") from exc

        return Response(status=status.HTTP_204_NO_CONTENT)
