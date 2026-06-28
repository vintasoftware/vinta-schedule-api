import datetime
from collections.abc import Callable
from typing import Annotated, Any, cast

from django.db.models import Case, IntegerField, Value, When
from django.http import Http404, HttpResponse

from allauth.socialaccount.models import SocialAccount
from dependency_injector.wiring import Provide, inject
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from audit.services import AuditService
from calendar_integration.constants import (
    CalendarProvider,
    CalendarSyncTriggerSource,
    CalendarType,
    CalendarVisibility,
    ExternalEventChangeRequestStatus,
)
from calendar_integration.exceptions import (
    CalendarGroupError,
    CalendarIntegrationError,
    ChangeRequestIneligibleError,
    ChangeRequestNotPendingError,
)
from calendar_integration.filtersets import (
    AvailableTimeFilterSet,
    BlockedTimeFilterSet,
    CalendarEventFilterSet,
    CalendarFilterSet,
    CalendarGroupFilterSet,
    ExternalEventChangeRequestFilterSet,
)
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    BookingPolicy,
    Calendar,
    CalendarEvent,
    CalendarGroup,
    CalendarOwnership,
    ExternalEventChangeRequest,
)
from calendar_integration.permissions import (
    BookingPolicyPermission,
    CalendarAvailabilityPermission,
    CalendarEventPermission,
    CalendarGroupPermission,
    ExternalEventChangeRequestPermission,
)
from calendar_integration.serializers import (
    AvailableTimeBatchSerializer,
    AvailableTimeBulkModificationSerializer,
    AvailableTimeRecurringExceptionSerializer,
    AvailableTimeSerializer,
    AvailableTimeWindowSerializer,
    BlockedTimeBulkModificationSerializer,
    BlockedTimeRecurringExceptionSerializer,
    BlockedTimeSerializer,
    BookableSlotProposalSerializer,
    BookingPolicySerializer,
    BulkBlockedTimeSerializer,
    CalendarBundleCreateSerializer,
    CalendarBundleUpdateSerializer,
    CalendarEventSerializer,
    CalendarEventTransferSerializer,
    CalendarGroupAvailabilityQuerySerializer,
    CalendarGroupEventCreateSerializer,
    CalendarGroupRangeAvailabilitySerializer,
    CalendarGroupSerializer,
    CalendarSerializer,
    CalendarSyncRequestSerializer,
    CalendarSyncSerializer,
    EventBulkModificationSerializer,
    EventRecurringExceptionSerializer,
    ExternalEventChangeRequestSerializer,
    ResourceCalendarCreateSerializer,
    UnavailableTimeWindowSerializer,
)
from calendar_integration.services.booking_policy_service import BookingPolicyService
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.external_event_change_request_service import (
    ExternalEventChangeRequestService,
)
from calendar_integration.services.ics_service import CalendarEventICSService
from common.utils.view_utils import ReadOnlyVintaScheduleModelViewSet, VintaScheduleModelViewSet
from organizations.models import get_active_organization_membership
from organizations.permissions import IsOrganizationAdmin


def _parse_bool(value, *, default: bool = True) -> bool:
    """Coerce a JSON/query value to bool, tolerating string forms ("true"/"false")."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


@extend_schema_view(
    list=extend_schema(
        summary="List calendars",
        parameters=[
            OpenApiParameter(
                name="include_unlisted",
                type=bool,
                location=OpenApiParameter.QUERY,
                required=False,
                description=(
                    "When true, include unlisted calendars (visibility=unlisted) in the response. "
                    "Unlisted calendars are hidden from booking queries but still synced. "
                    "Defaults to false."
                ),
            ),
            OpenApiParameter(
                name="include_inactive",
                type=bool,
                location=OpenApiParameter.QUERY,
                required=False,
                description=(
                    "When true, include inactive (soft-deleted) calendars (visibility=inactive). "
                    "Defaults to false."
                ),
            ),
            OpenApiParameter(
                name="owner",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description=(
                    "Scope the listing to a calendar owner. Pass 'me' to return only the "
                    "authenticated user's own calendars. Pass a numeric user id to return that "
                    "user's calendars — allowed for organization admins only; non-admins receive "
                    "403. When omitted, admins see all organization calendars while non-admins are "
                    "restricted to their own."
                ),
            ),
        ],
    ),
)
class CalendarViewSet(VintaScheduleModelViewSet):
    """
    ViewSet for managing calendars.
    """

    permission_classes = (CalendarAvailabilityPermission,)
    queryset = Calendar.objects.all()
    serializer_class = CalendarSerializer
    filterset_class = CalendarFilterSet

    def get_queryset(self):
        """Filter calendars by user's accessible calendar organizations.

        By default only active calendars are returned. Pass:
          ?include_unlisted=true  to also include unlisted calendars.
          ?include_inactive=true  to also include inactive (soft-deleted) calendars.
        Both flags can be combined.
        """
        user = self.request.user
        if not user.is_authenticated:
            return Calendar.original_manager.none()

        membership = get_active_organization_membership(user)
        if not membership:
            # Membership-less or inactive members get an empty queryset, not a 500.
            return Calendar.original_manager.none()

        qs = super().get_queryset().filter_by_organization(membership.organization_id)

        # For list, apply visibility filters driven by query params.
        # For all other actions (retrieve, update, destroy, custom actions) only exclude
        # inactive so unlisted calendars remain directly addressable by id.
        if self.action == "list":
            params = self.request.query_params

            # Owner scoping. Non-admins may only ever list calendars they own; admins see
            # all org calendars unless they narrow the listing with ?owner=.
            is_admin = user.is_organization_admin(membership.organization_id)
            owner = params.get("owner")
            if owner == "me":
                qs = qs.filter(ownerships__membership_user_id=user.id).distinct()
            elif owner:
                # A specific user id — admin-only; members cannot list others' calendars.
                if not is_admin:
                    raise PermissionDenied(
                        "Only organization admins can list other users' calendars."
                    )
                try:
                    owner_id = int(owner)
                except (TypeError, ValueError) as err:
                    raise ValidationError({"owner": "Must be 'me' or a numeric user id."}) from err
                qs = qs.filter(ownerships__membership_user_id=owner_id).distinct()
            elif not is_admin:
                # No owner filter + non-admin: restrict to the caller's own calendars.
                qs = qs.filter(ownerships__membership_user_id=user.id).distinct()

            include_unlisted = params.get("include_unlisted", "").lower() == "true"
            include_inactive = params.get("include_inactive", "").lower() == "true"

            if not include_inactive and not include_unlisted:
                qs = qs.filter(visibility=CalendarVisibility.ACTIVE)
            elif not include_inactive:
                qs = qs.exclude(visibility=CalendarVisibility.INACTIVE)
            elif not include_unlisted:
                qs = qs.exclude(visibility=CalendarVisibility.UNLISTED)
        else:
            include_inactive = (
                self.request.query_params.get("include_inactive", "").lower() == "true"
            )
            if not include_inactive:
                qs = qs.exclude_inactive()

        # active first, unlisted second, inactive last; sync-enabled before non-sync within each; stable tiebreak by id.
        visibility_order = Case(
            When(visibility=CalendarVisibility.ACTIVE, then=Value(0)),
            When(visibility=CalendarVisibility.UNLISTED, then=Value(1)),
            When(visibility=CalendarVisibility.INACTIVE, then=Value(2)),
            default=Value(3),
            output_field=IntegerField(),
        )
        return qs.annotate(visibility_order=visibility_order).order_by(
            "visibility_order", "-sync_enabled", "id"
        )

    @extend_schema(
        summary="Get the caller's default calendar",
        description=(
            "Returns the authenticated user's default calendar in their organization "
            "(the active CalendarOwnership flagged is_default). 404 when the user has no "
            "default calendar (e.g. before importing any calendars)."
        ),
        responses={
            200: CalendarSerializer,
            404: OpenApiResponse(description="No default calendar for this user"),
        },
    )
    @action(methods=["get"], detail=False, url_path="default", url_name="default")
    def default(self, request):
        """GET /calendar/default/ — the caller's own default calendar.

        Resolved via the user's ``CalendarOwnership`` with ``is_default=True`` in
        their active organization, restricted to active calendars. 404 if none.
        """
        membership = get_active_organization_membership(request.user)
        if not membership:
            raise NotFound("User has no default calendar.")

        ownership = (
            CalendarOwnership.objects.filter_by_organization(membership.organization_id)
            .filter(
                membership=membership,
                is_default=True,
                calendar__visibility=CalendarVisibility.ACTIVE,
            )
            .select_related("calendar")
            .order_by("id")
            .first()
        )
        if ownership is None or ownership.calendar is None:
            raise NotFound("User has no default calendar.")

        serializer = self.get_serializer(ownership.calendar)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Soft-disable a calendar",
        description=(
            "Disables a calendar by setting visibility=inactive instead of deleting the row. "
            "The row persists and is hidden from default list/detail queries. "
            "\n\n"
            "**Authorization rules (enforced after org-scoping):**\n"
            "- BUNDLE calendar: caller must be an org admin. Non-admin members receive 403.\n"
            "- Non-bundle calendar (PERSONAL/RESOURCE/VIRTUAL): caller must own the calendar "
            "(CalendarOwnership) or be an org admin. Non-owner non-admins receive 403.\n"
            "\n\n"
            "**Bundle semantics:** disabling a bundle sets only the bundle calendar inactive. "
            "Child calendars, bundle events, and their representation BlockedTimes/events are "
            "deliberately left untouched (event cancellation is out of scope; see plan Phase 11)."
        ),
        responses={204: None},
    )
    def destroy(self, request, *args, **kwargs):
        """Soft-disable the calendar (set visibility=inactive) instead of hard-deleting.

        Applies object-type-aware permission gating:
        - BUNDLE: admin-only (bundles are management resources).
        - Non-bundle: owner or admin.
        """
        calendar = self.get_object()

        if calendar.calendar_type == CalendarType.BUNDLE:
            # Bundle calendars are management resources — admin-only disable.
            # Intentionally: only the bundle wrapper is hidden; child calendars, bundle
            # events, and their representation BlockedTimes/events are left intact.
            # Event cancellation is explicitly out of scope (see plan Phase 11, Open Q #3:
            # "Leave events, hide bundle; surface a follow-up if cancellation is desired.").
            if not request.user.is_organization_admin(calendar.organization_id):
                raise PermissionDenied("Only org admins can disable a bundle calendar.")
        else:
            # Non-bundle calendars (PERSONAL/RESOURCE/VIRTUAL): owner or admin.
            is_owner = CalendarOwnership.objects.filter(
                calendar=calendar,
                membership_user_id=request.user.id,
                organization_id=calendar.organization_id,
            ).exists()
            is_admin = request.user.is_organization_admin(calendar.organization_id)
            if not (is_owner or is_admin):
                raise PermissionDenied(
                    "You must own this calendar or be an org admin to disable it."
                )

        calendar.visibility = CalendarVisibility.INACTIVE
        calendar.save(update_fields=["visibility"])
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=CalendarBundleCreateSerializer())
    @action(
        methods=["POST"],
        detail=False,
        url_path="bundle",
        url_name="bundle",
    )
    def create_bundle_calendar(self, request, *args, **kwargs):
        serializer = CalendarBundleCreateSerializer(
            data=request.data,
            context=self.get_serializer_context(),
        )

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        calendar_bundle = serializer.save()

        optimized_calendar_bundle = self.get_queryset().get(id=calendar_bundle.id)

        return Response(
            self.get_serializer_class()(instance=optimized_calendar_bundle).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="Create a manual resource calendar",
        description=(
            "Org admins create an internal (manual) resource calendar — a shared bookable "
            "resource (room, equipment, etc.) owned by the organization rather than synced "
            "from an external provider. Sets provider=internal and calendar_type=resource. "
            "Admin only. Returns the created calendar."
        ),
        request=ResourceCalendarCreateSerializer,
        responses={201: CalendarSerializer},
    )
    @action(
        methods=["post"],
        detail=False,
        url_path="resource",
        url_name="resource",
        permission_classes=[IsOrganizationAdmin],
    )
    def create_resource_calendar(self, request, *args, **kwargs):
        """POST /calendar/resource/ — admins create a manual resource calendar."""
        serializer = ResourceCalendarCreateSerializer(
            data=request.data,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        calendar = serializer.save()

        optimized_calendar = self.get_queryset().get(id=calendar.id)
        return Response(
            self.get_serializer_class()(instance=optimized_calendar).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="Update a bundle calendar's children and primary",
        description=(
            "Reconcile the child calendars and primary designation for an existing bundle. "
            "Provide the full desired set of bundle_calendars; children not in the list will "
            "be removed and new ones will be added. Optionally specify primary_calendar (must "
            "be one of bundle_calendars). Admin only. Returns the updated bundle calendar."
        ),
        request=CalendarBundleUpdateSerializer,
        responses={200: CalendarSerializer},
    )
    @action(
        methods=["patch"],
        detail=True,
        url_path="bundle",
        url_name="bundle-update",
        permission_classes=[IsOrganizationAdmin],
    )
    def update_bundle(self, request, pk: str | None = None) -> Response:
        """Update the children and primary calendar of an existing bundle (admin only)."""
        calendar = self.get_object()

        serializer = CalendarBundleUpdateSerializer(
            instance=calendar,
            data=request.data,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        updated_bundle = self.get_queryset().get(id=calendar.id)
        return Response(
            CalendarSerializer(instance=updated_bundle).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        summary="Request calendar import",
        description="Request import of external calendars for the authenticated user.",
        responses={202: {"type": "object", "properties": {"detail": {"type": "string"}}}},
    )
    @action(
        methods=["post"],
        detail=False,
        url_path="request-import",
        url_name="request-import",
    )
    @inject
    def request_import(
        self,
        request,
        calendar_service_factory: Annotated[
            Callable[[], CalendarService], Provide["calendar_service.provider"]
        ],
    ):
        """Request import of external calendars for the authenticated user."""
        user = request.user

        membership = get_active_organization_membership(user)
        if not membership:
            return Response(
                {"detail": "User is not an active member of any organization."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Only Google/Microsoft accounts carry calendars. Other connected
        # providers (e.g. a pure auth login) are ignored rather than aborting.
        social_accounts = list(
            SocialAccount.objects.filter(
                user=user,
                provider__in=[CalendarProvider.GOOGLE, CalendarProvider.MICROSOFT],
            )
        )
        if not social_accounts:
            return Response(
                {"detail": "User has no connected Google or Microsoft calendar account."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Import each account independently. A failure on one account (e.g. an
        # expired token with no refresh_token) must not abort the others — it is
        # reported under ``skipped`` so the caller knows which account to fix.
        # Whether to also sync events right after importing. Defaults to True to
        # preserve existing behavior; callers can pass false to only refresh the
        # calendar list without pulling events.
        sync_after_import = _parse_bool(request.data.get("sync_after_import", True))

        imported: list[int] = []
        skipped: list[dict] = []
        for social_account in social_accounts:
            try:
                fresh_service = calendar_service_factory()
                fresh_service.authenticate(
                    account=social_account,
                    organization=membership.organization,
                )
                fresh_service.request_calendars_import(sync_after_import=sync_after_import)
                imported.append(social_account.id)
            except (ValueError, CalendarIntegrationError) as e:
                skipped.append({"account_id": social_account.id, "reason": str(e)})

        if not imported:
            # Nothing could be imported — surface the per-account reasons (400).
            # Use a plain Response (not ValidationError) so the structured
            # ``skipped`` payload survives instead of being coerced to strings.
            return Response(
                {
                    "detail": (
                        "No calendar account could be imported. "
                        "Reconnect the account to grant calendar access."
                    ),
                    "skipped": skipped,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "detail": f"Calendar import requested for {len(imported)} account(s).",
                "imported": imported,
                "skipped": skipped,
            },
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        summary="Request calendar sync",
        description="Request synchronization of an owned calendar over a date range.",
        request=CalendarSyncRequestSerializer,
        responses={
            202: CalendarSyncSerializer(),
            409: OpenApiResponse(description="Sync is disabled for this calendar."),
        },
    )
    @action(
        methods=["post"],
        detail=True,
        url_path="request-sync",
        url_name="request-sync",
    )
    @inject
    def request_sync(
        self,
        request,
        pk=None,
        calendar_service: Annotated[CalendarService, Provide["calendar_service"]] = None,  # type: ignore
    ):
        """Request synchronization of an owned calendar over a date range."""
        calendar = self.get_object()
        user = request.user

        # Check ownership - user must own this calendar
        if not CalendarOwnership.objects.filter(
            calendar=calendar,
            membership_user_id=user.id,
            organization_id=calendar.organization_id,
        ).exists():
            return Response(
                {"detail": "You do not own this calendar."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Validate request input using serializer
        input_serializer = CalendarSyncRequestSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        start_datetime = data["start_datetime"]
        end_datetime = data["end_datetime"]
        should_update_events = data["should_update_events"]

        # Get social account for authentication
        social_account = SocialAccount.objects.filter(user=user, provider=calendar.provider).first()

        # Guard against missing social account
        if social_account is None:
            raise ValidationError(
                {
                    "non_field_errors": [
                        f"No linked account found for provider '{calendar.provider}'."
                    ]
                }
            )

        membership = get_active_organization_membership(user)
        if not membership:
            return Response(
                {"detail": "User is not an active member of any organization."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            calendar_service.authenticate(
                account=social_account,
                organization=membership.organization,
            )

            calendar_sync = calendar_service.request_calendar_sync(
                calendar=calendar,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                should_update_events=should_update_events,
            )

            if calendar_sync is None:
                return Response(
                    {"detail": "Sync is disabled for this calendar (sync_enabled is False)."},
                    status=status.HTTP_409_CONFLICT,
                )

            serializer = CalendarSyncSerializer(calendar_sync)
            return Response(serializer.data, status=status.HTTP_202_ACCEPTED)
        except (ValueError, CalendarIntegrationError, NotImplementedError) as e:
            raise ValidationError({"non_field_errors": [str(e)]}) from e

    @extend_schema(
        summary="Admin syncs another user's calendar",
        description="Admin syncs any calendar in the organization over a date range.",
        request=CalendarSyncRequestSerializer,
        responses={
            202: CalendarSyncSerializer(),
            409: OpenApiResponse(description="Sync is disabled for this calendar."),
        },
    )
    @action(
        methods=["post"],
        detail=True,
        url_path="admin-sync",
        url_name="admin-sync",
        permission_classes=[IsOrganizationAdmin],
    )
    @inject
    def admin_sync(
        self,
        request,
        pk=None,
        calendar_service: Annotated[CalendarService, Provide["calendar_service"]] = None,  # type: ignore
    ):
        """Admin syncs any calendar in the organization over a date range."""
        calendar = self.get_object()  # org-scoped via get_queryset
        user = request.user

        # Validate request input using serializer
        input_serializer = CalendarSyncRequestSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        start_datetime = data["start_datetime"]
        end_datetime = data["end_datetime"]
        should_update_events = data["should_update_events"]

        # Resolve the calendar's owner via CalendarOwnership (membership-backed only;
        # orphan ownerships with a null membership cannot resolve an owner).
        # Use the default owner if multiple owners exist; else the first
        ownership = (
            CalendarOwnership.objects.filter(
                calendar=calendar,
                organization_id=calendar.organization_id,
                membership_user_id__isnull=False,
            )
            .order_by("-is_default", "id")
            .first()
        )

        if not ownership:
            return Response(
                {"detail": "Calendar has no owner; cannot sync."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Resolve the owner's SocialAccount for the calendar's provider
        owner_social_account = SocialAccount.objects.filter(
            user_id=ownership.membership_user_id, provider=calendar.provider
        ).first()

        if not owner_social_account:
            return Response(
                {
                    "detail": f"Calendar owner has no linked {calendar.provider} account; cannot sync."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Get the admin's organization membership (already checked by IsOrganizationAdmin)
        membership = get_active_organization_membership(user)
        if not membership:
            return Response(
                {"detail": "User is not an active member of any organization."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            # Authenticate with the OWNER's account, not the admin's
            calendar_service.authenticate(
                account=owner_social_account,
                organization=membership.organization,
            )

            calendar_sync = calendar_service.request_calendar_sync(
                calendar=calendar,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                should_update_events=should_update_events,
                trigger_source=CalendarSyncTriggerSource.ADMIN,
            )

            if calendar_sync is None:
                return Response(
                    {"detail": "Sync is disabled for this calendar (sync_enabled is False)."},
                    status=status.HTTP_409_CONFLICT,
                )

            serializer = CalendarSyncSerializer(calendar_sync)
            return Response(serializer.data, status=status.HTTP_202_ACCEPTED)
        except (ValueError, CalendarIntegrationError, NotImplementedError) as e:
            raise ValidationError({"non_field_errors": [str(e)]}) from e

    @extend_schema(
        summary="Get available time windows",
        description="Get available time windows for a calendar within a specified date range.",
        parameters=[
            OpenApiParameter(
                name="start_datetime",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Start datetime in ISO format (YYYY-MM-DDTHH:MM:SS)",
                required=True,
            ),
            OpenApiParameter(
                name="end_datetime",
                type=str,
                location=OpenApiParameter.QUERY,
                description="End datetime in ISO format (YYYY-MM-DDTHH:MM:SS)",
                required=True,
            ),
        ],
        responses={200: AvailableTimeWindowSerializer(many=True)},
    )
    @action(
        methods=["get"],
        detail=True,
        url_path="available-windows",
        url_name="available-windows",
        pagination_class=None,  # returns a bare array, not a paginated page
    )
    @inject
    def available_windows(
        self,
        request,
        pk,
        calendar_service: Annotated[CalendarService, Provide["calendar_service"]],
    ):
        """
        Get available time windows for a calendar.
        """
        calendar = self.get_object()

        start_datetime_str = request.query_params.get("start_datetime")
        end_datetime_str = request.query_params.get("end_datetime")

        if not start_datetime_str or not end_datetime_str:
            raise ValidationError(
                {"non_field_errors": ["start_datetime and end_datetime are required"]}
            )

        try:
            start_datetime = datetime.datetime.fromisoformat(
                start_datetime_str.replace("Z", "+00:00")
            )
            end_datetime = datetime.datetime.fromisoformat(end_datetime_str.replace("Z", "+00:00"))
        except (ValueError, CalendarIntegrationError) as e:
            raise ValidationError({"non_field_errors": [str(e)]}) from e

        # Get social account for authentication
        social_account = SocialAccount.objects.filter(
            user=request.user, provider=calendar.provider
        ).first()

        try:
            calendar_service.authenticate(
                account=social_account,
                organization=calendar.organization,
            )

            available_windows = calendar_service.get_availability_windows_in_range(
                calendar=calendar,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
            )

            serializer = AvailableTimeWindowSerializer(available_windows, many=True)
            return Response(serializer.data)
        except (ValueError, CalendarIntegrationError) as e:
            raise ValidationError({"non_field_errors": [str(e)]}) from e

    @extend_schema(
        summary="Get unavailable time windows",
        description="Get unavailable time windows for a calendar within a specified date range.",
        parameters=[
            OpenApiParameter(
                name="start_datetime",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Start datetime in ISO format (YYYY-MM-DDTHH:MM:SS)",
                required=True,
            ),
            OpenApiParameter(
                name="end_datetime",
                type=str,
                location=OpenApiParameter.QUERY,
                description="End datetime in ISO format (YYYY-MM-DDTHH:MM:SS)",
                required=True,
            ),
        ],
        responses={200: UnavailableTimeWindowSerializer(many=True)},
    )
    @action(
        methods=["get"],
        detail=True,
        url_path="unavailable-windows",
        url_name="unavailable-windows",
        pagination_class=None,  # returns a bare array, not a paginated page
    )
    @inject
    def unavailable_windows(
        self,
        request,
        pk,
        calendar_service: Annotated[CalendarService, Provide["calendar_service"]],
    ):
        """
        Get unavailable time windows for a calendar.
        """
        calendar = self.get_object()

        start_datetime_str = request.query_params.get("start_datetime")
        end_datetime_str = request.query_params.get("end_datetime")

        if not start_datetime_str or not end_datetime_str:
            raise ValidationError(
                {"non_field_errors": ["start_datetime and end_datetime are required"]}
            )

        try:
            start_datetime = datetime.datetime.fromisoformat(
                start_datetime_str.replace("Z", "+00:00")
            )
            end_datetime = datetime.datetime.fromisoformat(end_datetime_str.replace("Z", "+00:00"))
        except ValueError as e:
            raise ValidationError(
                {
                    "non_field_errors": [
                        "Invalid datetime format. Use ISO format (YYYY-MM-DDTHH:MM:SS)"
                    ]
                }
            ) from e

        try:
            # Get social account for authentication
            social_account = SocialAccount.objects.filter(
                user=request.user, provider=calendar.provider
            ).first()

            calendar_service.authenticate(
                account=social_account,
                organization=calendar.organization,
            )

            unavailable_windows = calendar_service.get_unavailable_time_windows_in_range(
                calendar=calendar,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
            )

            serializer = UnavailableTimeWindowSerializer(unavailable_windows, many=True)
            return Response(serializer.data)
        except (ValueError, CalendarIntegrationError) as e:
            raise ValidationError({"non_field_errors": [str(e)]}) from e


class CalendarEventViewSet(VintaScheduleModelViewSet):
    """
    ViewSet for managing calendar events.
    """

    filterset_class = CalendarEventFilterSet
    permission_classes = (CalendarEventPermission,)
    queryset = CalendarEvent.objects.all()
    serializer_class = CalendarEventSerializer

    def get_queryset(self):
        """
        Filter events by calendar organization of the authenticated user.

        Returns an empty queryset for membership-less or inactive-membership users
        rather than raising Http404, so the response is a clean empty list /
        404-on-object rather than a 500.
        """
        membership = get_active_organization_membership(self.request.user)
        if not membership:
            return CalendarEvent.original_manager.none()
        return super().get_queryset().filter_by_organization(membership.organization_id)

    def perform_create(self, serializer):
        # Surface domain errors (e.g. no available time window, invalid timezone)
        # as a 400 instead of leaking a 500 from the service layer.
        try:
            super().perform_create(serializer)
        except (ValueError, CalendarIntegrationError) as e:
            raise ValidationError({"non_field_errors": [str(e)]}) from e

    def perform_update(self, serializer):
        try:
            super().perform_update(serializer)
        except (ValueError, CalendarIntegrationError) as e:
            raise ValidationError({"non_field_errors": [str(e)]}) from e

    @extend_schema(
        summary="Delete calendar event",
        description="Delete a calendar event.",
        responses={204: None},
    )
    @inject
    def destroy(
        self,
        request,
        *args,
        calendar_service: Annotated[CalendarService, Provide["calendar_service"]],
        **kwargs,
    ):
        """
        Delete a calendar event using the calendar service.
        """
        instance = self.get_object()

        try:
            calendar_service.authenticate(
                account=SocialAccount.objects.get(
                    user=request.user, provider=instance.calendar.provider
                ),
                organization=instance.organization,
            )
            calendar_service.delete_event(
                calendar_id=instance.calendar.id,
                event_id=instance.id,
            )
            return Response(status=status.HTTP_204_NO_CONTENT)
        except (ValueError, CalendarIntegrationError) as e:
            raise ValidationError({"non_field_errors": [str(e)]}) from e

    @extend_schema(
        summary="Create recurring event exception",
        description="Create an exception for a recurring event (either cancelled or modified).",
        request=EventRecurringExceptionSerializer,
        responses={
            201: CalendarEventSerializer,
            204: None,
        },
    )
    @action(
        methods=["POST"],
        detail=True,
        url_path="create-exception",
        url_name="create-exception",
    )
    @inject
    def create_exception(
        self,
        request,
        pk,
        calendar_service: Annotated[CalendarService, Provide["calendar_service"]],
    ):
        """
        Create an exception for a recurring event.
        """
        parent_event = self.get_object()

        if not parent_event.is_recurring:
            raise ValidationError({"non_field_errors": ["Event is not a recurring event"]})

        serializer = EventRecurringExceptionSerializer(
            data=request.data,
            context={"request": request, "parent_event": parent_event},
        )
        serializer.is_valid(raise_exception=True)

        try:
            serializer.save()

            if serializer.instance is None:
                # Event was cancelled
                return Response(status=status.HTTP_204_NO_CONTENT)
            else:
                # Event was modified
                return Response(
                    CalendarEventSerializer(
                        serializer.instance,
                        context=self.get_serializer_context(),
                    ).data,
                    status=status.HTTP_201_CREATED,
                )
        except (ValueError, CalendarIntegrationError) as e:
            raise ValidationError({"non_field_errors": [str(e)]}) from e

    @extend_schema(
        summary="Bulk modify or cancel recurring event from a date",
        request=EventBulkModificationSerializer,
        responses={200: CalendarEventSerializer, 204: None},
    )
    @action(
        methods=["POST"],
        detail=True,
        url_path="bulk-modify",
        url_name="bulk-modify",
    )
    @inject
    def bulk_modify(
        self,
        request,
        pk,
        calendar_service: Annotated[CalendarService, Provide["calendar_service"]],
    ):
        parent_event = self.get_object()

        if not parent_event.is_recurring:
            raise ValidationError({"non_field_errors": ["Event is not a recurring event"]})

        serializer = EventBulkModificationSerializer(
            data=request.data,
            context={
                "request": request,
                "parent_event": parent_event,
                "calendar_service": calendar_service,
            },
        )
        serializer.is_valid(raise_exception=True)

        try:
            result = serializer.save()
            if result is None:
                return Response(status=status.HTTP_204_NO_CONTENT)
            return Response(
                CalendarEventSerializer(result, context=self.get_serializer_context()).data,
                status=status.HTTP_200_OK,
            )
        except (ValueError, CalendarIntegrationError) as e:
            raise ValidationError({"non_field_errors": [str(e)]}) from e

    @extend_schema(
        summary="Get expanded calendar events",
        parameters=[
            OpenApiParameter(
                name="calendar_id",
                type=int,
                location=OpenApiParameter.QUERY,
                required=True,
                description="Calendar ID to get events for",
            ),
            OpenApiParameter(
                name="start_time",
                type=str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="Start datetime for the range (ISO format)",
            ),
            OpenApiParameter(
                name="end_time",
                type=str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="End datetime for the range (ISO format)",
            ),
        ],
        responses={200: CalendarEventSerializer(many=True)},
    )
    @action(
        methods=["GET"],
        detail=False,
        url_path="expanded",
        url_name="expanded",
    )
    @inject
    def expanded(
        self,
        request,
        calendar_service: Annotated[CalendarService, Provide["calendar_service"]],
    ) -> Response:
        """Get expanded calendar events including materialized recurring instances."""
        calendar_id = request.query_params.get("calendar_id")
        start_datetime = request.query_params.get("start_time")
        end_datetime = request.query_params.get("end_time")

        if not all([calendar_id, start_datetime, end_datetime]):
            raise ValidationError(
                {"non_field_errors": ["calendar_id, start_time, and end_time are required"]}
            )

        membership = get_active_organization_membership(request.user)
        if not membership:
            return Response([], status=status.HTTP_200_OK)

        try:
            calendar = Calendar.objects.filter_by_organization(membership.organization.id).get(
                id=calendar_id
            )
        except Calendar.DoesNotExist as e:
            raise Http404("Calendar not found") from e

        try:
            start_dt = datetime.datetime.fromisoformat(start_datetime.replace("Z", "+00:00"))
            end_dt = datetime.datetime.fromisoformat(end_datetime.replace("Z", "+00:00"))
        except ValueError as e:
            raise ValidationError({"non_field_errors": ["Invalid datetime format"]}) from e

        calendar_service.initialize_without_provider(organization=membership.organization)

        # Pass the serializer's optimizer so recurring masters are prefetched; their
        # generated (pk-less) occurrences reuse that cache (see
        # get_calendar_events_expanded).
        context = self.get_serializer_context()
        expanded_events = calendar_service.get_calendar_events_expanded(
            calendar=calendar,
            start_date=start_dt,
            end_date=end_dt,
            optimize_queryset=CalendarEventSerializer(context=context).get_optimized_queryset,
        )

        # Real (pk-backed) events are re-fetched through the optimized queryset so
        # their nested relations are prefetched; generated occurrences (pk=None)
        # already carry their master's cache. Keeps the endpoint within the query
        # budget regardless of how events were produced.
        real_ids = [event.id for event in expanded_events if event.id is not None]
        if real_ids:
            optimized_by_id = {
                event.id: event
                for event in CalendarEventSerializer(context=context).get_optimized_queryset(
                    CalendarEvent.objects.filter_by_organization(membership.organization.id).filter(
                        id__in=real_ids
                    )
                )
            }
            expanded_events = [
                optimized_by_id.get(event.id, event) if event.id is not None else event
                for event in expanded_events
            ]

        serializer = CalendarEventSerializer(expanded_events, many=True, context=context)
        return Response(serializer.data)

    @extend_schema(
        summary="Transfer event to another calendar (admin)",
        description=(
            "Move an event from its current calendar to a target calendar within the same "
            "organization. The service authenticates with the SOURCE calendar owner's credentials "
            "to read and delete the event from the provider. Admin only."
        ),
        request=CalendarEventTransferSerializer,
        responses={200: CalendarEventSerializer},
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="transfer",
        url_name="transfer",
        permission_classes=[IsOrganizationAdmin],
    )
    @inject
    def transfer(
        self,
        request,
        pk: str | None = None,
        calendar_service: Annotated[CalendarService, Provide["calendar_service"]] = None,  # type: ignore
    ) -> Response:
        """Transfer an event to a different calendar (admin-only)."""
        event = self.get_object()  # org-scoped → cross-org yields 404; non-admin → 403

        input_serializer = CalendarEventTransferSerializer(
            data=request.data, context={**self.get_serializer_context(), "event": event}
        )
        input_serializer.is_valid(raise_exception=True)
        target_calendar = input_serializer.validated_data["target_calendar_id"]

        # --- Authenticate with the SOURCE calendar owner's credentials ---
        source_calendar = event.calendar
        ownership = (
            CalendarOwnership.objects.filter(
                calendar=source_calendar,
                organization_id=source_calendar.organization_id,
                membership_user_id__isnull=False,
            )
            .order_by("-is_default", "id")
            .first()
        )

        if not ownership:
            return Response(
                {"detail": "Source calendar has no owner; cannot read from provider."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        owner_social_account = SocialAccount.objects.filter(
            user_id=ownership.membership_user_id, provider=source_calendar.provider
        ).first()

        if not owner_social_account:
            return Response(
                {
                    "detail": (
                        f"Source calendar owner has no linked {source_calendar.provider} account; "
                        "cannot transfer event."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Use admin's organization for service context
        membership = get_active_organization_membership(request.user)
        if not membership:
            return Response(
                {"detail": "User is not an active member of any organization."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            calendar_service.authenticate(
                account=owner_social_account,
                organization=membership.organization,
            )
            new_event = calendar_service.transfer_event(
                event=event,
                new_calendar=target_calendar,
            )
            return Response(
                CalendarEventSerializer(new_event, context=self.get_serializer_context()).data,
                status=status.HTTP_200_OK,
            )
        except (ValueError, CalendarIntegrationError, NotImplementedError) as e:
            raise ValidationError({"non_field_errors": [str(e)]}) from e

    @extend_schema(
        summary="Download calendar event ICS",
        responses={(200, "text/calendar"): OpenApiTypes.BINARY},
    )
    @action(detail=True, methods=["get"], url_path="ics", url_name="ics")
    def download_ics(self, request, *args, **kwargs):
        """Download a calendar event as an iCalendar (.ics) file.

        Returns the event in RFC 5545 format with proper timezone handling,
        recurrence rules (if applicable), and attendee information.
        """
        # Resolve the event with get_object() for org scoping and permission checks.
        event = self.get_object()

        # Re-fetch the event with the required prefetch set from the ICS service
        # docstring to avoid N+1 queries during ICS generation. Reuse the viewset's
        # canonical org-scoped queryset (get_object() above already owns the 404/403)
        # and key the re-fetch by the authorized pk.
        event = (
            self.get_queryset()
            .select_related("calendar")
            .prefetch_related(
                "calendar__ownerships__membership__user",
                "attendances__membership__user",
                "external_attendances__external_attendee",
                "recurrence_rule",
                "recurrence_exceptions",
            )
            .get(pk=event.id)
        )

        # Generate the ICS bytes using the service.
        ics_bytes = CalendarEventICSService().build_ics(event)

        # Return as an HTTP response with proper content type and attachment header.
        response = HttpResponse(ics_bytes, content_type="text/calendar; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="event-{event.id}.ics"'
        return response


class BlockedTimeViewSet(VintaScheduleModelViewSet):
    """
    ViewSet for managing blocked times with recurring support.
    """

    permission_classes = (CalendarAvailabilityPermission,)
    queryset = BlockedTime.objects.all()
    serializer_class = BlockedTimeSerializer
    filterset_class = BlockedTimeFilterSet

    def get_queryset(self):
        """Filter blocked times by user's accessible calendar organizations."""
        user = self.request.user
        if not user.is_authenticated:
            return BlockedTime.original_manager.none()

        membership = get_active_organization_membership(user)
        if not membership:
            return BlockedTime.original_manager.none()

        # `super().get_queryset()` runs the VirtualModel optimization (prefetches
        # `calendar`, etc.) — without it the `calendar` PrimaryKeyRelatedField loads
        # one Calendar row per BlockedTime and trips the serializer query budget.
        return super().get_queryset().filter_by_organization(membership.organization.id)

    @extend_schema(
        summary="Create bulk blocked times",
        request=BulkBlockedTimeSerializer,
        responses={201: BlockedTimeSerializer(many=True)},
    )
    @action(
        methods=["POST"],
        detail=False,
        url_path="bulk-create",
        url_name="bulk-create",
    )
    def bulk_create(self, request):
        """Create multiple blocked times."""
        serializer = BulkBlockedTimeSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        blocked_times = serializer.save()

        # Re-fetch through the optimized queryset so nested relations are prefetched
        # (created rows expose `calendar` as a composite FK that loads per row).
        context = self.get_serializer_context()
        if not blocked_times:
            return Response([], status=status.HTTP_201_CREATED)
        ids = [bt.id for bt in blocked_times]
        optimized_by_id = {
            bt.id: bt
            for bt in BlockedTimeSerializer(context=context)
            .get_optimized_queryset(
                BlockedTime.objects.filter_by_organization(blocked_times[0].organization_id)
            )
            .filter(id__in=ids)
        }
        ordered = [optimized_by_id[bt.id] for bt in blocked_times if bt.id in optimized_by_id]

        return Response(
            BlockedTimeSerializer(ordered, many=True, context=context).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="Get expanded blocked times",
        parameters=[
            OpenApiParameter(
                name="calendar_id",
                type=int,
                location=OpenApiParameter.QUERY,
                required=True,
                description="Calendar ID to get blocked times for",
            ),
            OpenApiParameter(
                name="start_datetime",
                type=str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="Start datetime for the range (ISO format)",
            ),
            OpenApiParameter(
                name="end_datetime",
                type=str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="End datetime for the range (ISO format)",
            ),
        ],
        responses={200: BlockedTimeSerializer(many=True)},
    )
    @action(
        methods=["GET"],
        detail=False,
        url_path="expanded",
        url_name="expanded",
    )
    @inject
    def expanded(
        self,
        request,
        calendar_service: Annotated[CalendarService, Provide["calendar_service"]],
    ):
        """Get expanded blocked times including recurring instances."""
        calendar_id = request.query_params.get("calendar_id")
        start_datetime = request.query_params.get("start_time")
        end_datetime = request.query_params.get("end_time")

        if not all([calendar_id, start_datetime, end_datetime]):
            raise ValidationError(
                {"non_field_errors": ["calendar_id, start_time, and end_time are required"]}
            )

        membership = get_active_organization_membership(request.user)
        if not membership:
            return Response([], status=status.HTTP_200_OK)

        try:
            calendar = Calendar.objects.filter_by_organization(membership.organization.id).get(
                id=calendar_id
            )
        except Calendar.DoesNotExist as e:
            raise Http404("Calendar not found") from e

        try:
            start_dt = datetime.datetime.fromisoformat(start_datetime.replace("Z", "+00:00"))
            end_dt = datetime.datetime.fromisoformat(end_datetime.replace("Z", "+00:00"))
        except ValueError as e:
            raise ValidationError({"non_field_errors": ["Invalid datetime format"]}) from e

        calendar_service.initialize_without_provider(organization=membership.organization)

        expanded_blocked_times = calendar_service.get_blocked_times_expanded(
            calendar=calendar,
            start_date=start_dt,
            end_date=end_dt,
        )

        serializer = BlockedTimeSerializer(
            expanded_blocked_times, many=True, context=self.get_serializer_context()
        )
        return Response(serializer.data)

    @extend_schema(
        summary="Create recurring blocked time exception",
        description="Create an exception for a recurring blocked time (either cancelled or modified).",
        request=BlockedTimeRecurringExceptionSerializer,
        responses={
            201: BlockedTimeSerializer,
            204: None,
        },
    )
    @action(
        methods=["POST"],
        detail=True,
        url_path="create-exception",
        url_name="create-exception",
    )
    def create_exception(
        self,
        request,
        pk,
    ):
        """
        Create an exception for a recurring blocked time.
        """
        parent_blocked_time = self.get_object()

        if not parent_blocked_time.is_recurring:
            raise ValidationError({"non_field_errors": ["Blocked time is not recurring"]})

        serializer = BlockedTimeRecurringExceptionSerializer(
            data=request.data,
            context={"request": request, "parent_blocked_time": parent_blocked_time},
        )
        serializer.is_valid(raise_exception=True)

        try:
            serializer.save()

            if serializer.instance is None:
                # Blocked time was cancelled
                return Response(status=status.HTTP_204_NO_CONTENT)
            else:
                # Blocked time was modified
                return Response(
                    BlockedTimeSerializer(
                        serializer.instance,
                        context=self.get_serializer_context(),
                    ).data,
                    status=status.HTTP_201_CREATED,
                )
        except (ValueError, CalendarIntegrationError) as e:
            raise ValidationError({"non_field_errors": [str(e)]}) from e

    @extend_schema(
        summary="Bulk modify or cancel recurring blocked time from a date",
        request=BlockedTimeBulkModificationSerializer,
        responses={200: BlockedTimeSerializer, 204: None},
    )
    @action(
        methods=["POST"],
        detail=True,
        url_path="bulk-modify",
        url_name="bulk-modify",
    )
    @inject
    def bulk_modify(
        self,
        request,
        pk,
        calendar_service: Annotated[CalendarService, Provide["calendar_service"]],
    ):
        parent_blocked_time = self.get_object()

        if not parent_blocked_time.is_recurring:
            raise ValidationError({"non_field_errors": ["Blocked time is not recurring"]})

        serializer = BlockedTimeBulkModificationSerializer(
            data=request.data,
            context={
                "request": request,
                "parent_blocked_time": parent_blocked_time,
                "calendar_service": calendar_service,
            },
        )
        serializer.is_valid(raise_exception=True)

        try:
            result = serializer.save()
            if result is None:
                return Response(status=status.HTTP_204_NO_CONTENT)
            return Response(
                BlockedTimeSerializer(result, context=self.get_serializer_context()).data,
                status=status.HTTP_200_OK,
            )
        except ValueError as e:
            raise ValidationError({"non_field_errors": [str(e)]}) from e


class AvailableTimeViewSet(VintaScheduleModelViewSet):
    """
    ViewSet for managing available times with recurring support.
    """

    permission_classes = (CalendarAvailabilityPermission,)
    queryset = AvailableTime.objects.all()
    serializer_class = AvailableTimeSerializer
    filterset_class = AvailableTimeFilterSet

    def get_queryset(self):
        """Filter available times by user's accessible calendar organizations."""
        user = self.request.user
        if not user.is_authenticated:
            return AvailableTime.original_manager.none()

        membership = get_active_organization_membership(user)
        if not membership:
            return AvailableTime.original_manager.none()

        # See BlockedTimeViewSet.get_queryset: `super()` applies the VirtualModel
        # optimization so the `calendar` relation is prefetched, not loaded per row.
        return super().get_queryset().filter_by_organization(membership.organization.id)

    @extend_schema(
        summary="Batch create/update/delete available times",
        description=(
            "Apply a list of create/update/delete operations to a single calendar's "
            "available times in one transaction (all-or-nothing). The calendar defaults "
            "to the user's default calendar when omitted. Returns the calendar's "
            "available times after the batch."
        ),
        request=AvailableTimeBatchSerializer,
        responses={200: AvailableTimeSerializer(many=True)},
    )
    @action(
        methods=["POST"],
        detail=False,
        url_path="batch",
        url_name="batch",
    )
    def batch(self, request):
        """Transactionally create/update/delete available times for a calendar."""
        serializer = AvailableTimeBatchSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        calendar = serializer.save()

        # Read back the calendar's resulting set through the optimized queryset so
        # nested relations are prefetched (composite `calendar` FK loads per row).
        context = self.get_serializer_context()
        resulting = (
            AvailableTimeSerializer(context=context)
            .get_optimized_queryset(
                AvailableTime.objects.filter_by_organization(calendar.organization_id)
            )
            .filter(calendar_fk=calendar)
        )
        return Response(
            AvailableTimeSerializer(list(resulting), many=True, context=context).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        summary="Get expanded available times",
        parameters=[
            OpenApiParameter(
                name="calendar_id",
                type=int,
                location=OpenApiParameter.QUERY,
                required=True,
                description="Calendar ID to get available times for",
            ),
            OpenApiParameter(
                name="start_datetime",
                type=str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="Start datetime for the range (ISO format)",
            ),
            OpenApiParameter(
                name="end_datetime",
                type=str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="End datetime for the range (ISO format)",
            ),
        ],
        responses={200: AvailableTimeSerializer(many=True)},
    )
    @action(
        methods=["GET"],
        detail=False,
        url_path="expanded",
        url_name="expanded",
    )
    @inject
    def expanded(
        self,
        request,
        calendar_service: Annotated[CalendarService, Provide["calendar_service"]],
    ):
        """Get expanded available times including recurring instances."""
        calendar_id = request.query_params.get("calendar_id")
        start_datetime = request.query_params.get("start_time")
        end_datetime = request.query_params.get("end_time")

        if not all([calendar_id, start_datetime, end_datetime]):
            raise ValidationError(
                {"non_field_errors": ["calendar_id, start_time, and end_time are required"]}
            )

        membership = get_active_organization_membership(request.user)
        if not membership:
            return Response([], status=status.HTTP_200_OK)

        try:
            calendar = Calendar.objects.filter_by_organization(membership.organization.id).get(
                id=calendar_id
            )
        except Calendar.DoesNotExist as e:
            raise Http404("Calendar not found") from e

        try:
            start_dt = datetime.datetime.fromisoformat(start_datetime.replace("Z", "+00:00"))
            end_dt = datetime.datetime.fromisoformat(end_datetime.replace("Z", "+00:00"))
        except ValueError as e:
            raise ValidationError({"non_field_errors": ["Invalid datetime format"]}) from e

        calendar_service.initialize_without_provider(organization=membership.organization)

        expanded_available_times = calendar_service.get_available_times_expanded(
            calendar=calendar,
            start_date=start_dt,
            end_date=end_dt,
        )

        serializer = AvailableTimeSerializer(
            expanded_available_times, many=True, context=self.get_serializer_context()
        )
        return Response(serializer.data)

    @extend_schema(
        summary="Create recurring available time exception",
        description="Create an exception for a recurring available time (either cancelled or modified).",
        request=AvailableTimeRecurringExceptionSerializer,
        responses={
            201: AvailableTimeSerializer,
            204: None,
        },
    )
    @action(
        methods=["POST"],
        detail=True,
        url_path="create-exception",
        url_name="create-exception",
    )
    def create_exception(
        self,
        request,
        pk,
    ):
        """
        Create an exception for a recurring available time.
        """
        parent_available_time = self.get_object()

        if not parent_available_time.is_recurring:
            raise ValidationError({"non_field_errors": ["Available time is not recurring"]})

        serializer = AvailableTimeRecurringExceptionSerializer(
            data=request.data,
            context={"request": request, "parent_available_time": parent_available_time},
        )
        serializer.is_valid(raise_exception=True)

        try:
            serializer.save()

            if serializer.instance is None:
                # Available time was cancelled
                return Response(status=status.HTTP_204_NO_CONTENT)
            else:
                # Available time was modified
                return Response(
                    AvailableTimeSerializer(
                        serializer.instance,
                        context=self.get_serializer_context(),
                    ).data,
                    status=status.HTTP_201_CREATED,
                )
        except ValueError as e:
            raise ValidationError({"non_field_errors": [str(e)]}) from e

    @extend_schema(
        summary="Bulk modify or cancel recurring available time from a date",
        request=AvailableTimeBulkModificationSerializer,
        responses={200: AvailableTimeSerializer, 204: None},
    )
    @action(
        methods=["POST"],
        detail=True,
        url_path="bulk-modify",
        url_name="bulk-modify",
    )
    @inject
    def bulk_modify(
        self,
        request,
        pk,
        calendar_service: Annotated[CalendarService, Provide["calendar_service"]],
    ):
        parent_available_time = self.get_object()

        if not parent_available_time.is_recurring:
            raise ValidationError({"non_field_errors": ["Available time is not recurring"]})

        serializer = AvailableTimeBulkModificationSerializer(
            data=request.data,
            context={
                "request": request,
                "parent_available_time": parent_available_time,
                "calendar_service": calendar_service,
            },
        )
        serializer.is_valid(raise_exception=True)

        try:
            result = serializer.save()
            if result is None:
                return Response(status=status.HTTP_204_NO_CONTENT)
            return Response(
                AvailableTimeSerializer(result, context=self.get_serializer_context()).data,
                status=status.HTTP_200_OK,
            )
        except ValueError as e:
            raise ValidationError({"non_field_errors": [str(e)]}) from e


class CalendarGroupViewSet(VintaScheduleModelViewSet):
    """
    ViewSet for CalendarGroup CRUD and grouped event actions.
    """

    permission_classes = (CalendarGroupPermission,)
    queryset = CalendarGroup.objects.all()
    serializer_class = CalendarGroupSerializer
    filterset_class = CalendarGroupFilterSet

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return CalendarGroup.original_manager.none()
        membership = get_active_organization_membership(user)
        if not membership:
            return CalendarGroup.original_manager.none()
        return super().get_queryset().filter_by_organization(membership.organization_id)

    @extend_schema(
        summary="Delete calendar group",
        description="Delete a CalendarGroup. Fails with 400 if the group has any bookings.",
        responses={204: None},
    )
    @inject
    def destroy(
        self,
        request,
        *args,
        calendar_group_service: Annotated[CalendarGroupService, Provide["calendar_group_service"]],
        **kwargs,
    ):
        instance = self.get_object()
        calendar_group_service.initialize(organization=instance.organization)
        try:
            calendar_group_service.delete_group(group_id=instance.id)
        except CalendarGroupError as e:
            raise ValidationError({"non_field_errors": [str(e)]}) from e
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        summary="Create grouped event",
        request=CalendarGroupEventCreateSerializer,
        responses={201: CalendarEventSerializer},
    )
    @action(
        methods=["POST"],
        detail=True,
        url_path="events",
        url_name="create-event",
    )
    def create_event(self, request, pk):
        group = self.get_object()
        serializer = CalendarGroupEventCreateSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        event = serializer.save(group=group)
        # Re-fetch through the serializer's optimized queryset so nested
        # attendances/resource relations are prefetched (avoids the query-budget N+1).
        context = self.get_serializer_context()
        optimized_event = (
            CalendarEventSerializer(context=context)
            .get_optimized_queryset(
                CalendarEvent.objects.filter_by_organization(group.organization_id)
            )
            .get(id=event.id)
        )
        return Response(
            CalendarEventSerializer(optimized_event, context=context).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="List events booked under this group",
        parameters=[
            OpenApiParameter(
                name="start_datetime",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Start datetime in ISO format",
                required=True,
            ),
            OpenApiParameter(
                name="end_datetime",
                type=str,
                location=OpenApiParameter.QUERY,
                description="End datetime in ISO format",
                required=True,
            ),
        ],
        responses={200: CalendarEventSerializer(many=True)},
    )
    @action(
        methods=["GET"],
        detail=True,
        url_path="booked-events",
        url_name="list-events",
    )
    @inject
    def list_events(
        self,
        request,
        pk,
        calendar_group_service: Annotated[CalendarGroupService, Provide["calendar_group_service"]],
    ):
        group = self.get_object()
        start_raw = request.query_params.get("start_datetime")
        end_raw = request.query_params.get("end_datetime")
        if not start_raw or not end_raw:
            raise ValidationError(
                {"non_field_errors": ["start_datetime and end_datetime are required"]}
            )
        try:
            start_dt = datetime.datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            end_dt = datetime.datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        except ValueError as e:
            raise ValidationError(
                {"non_field_errors": ["Invalid datetime format; use ISO 8601."]}
            ) from e

        calendar_group_service.initialize(organization=group.organization)
        events = calendar_group_service.get_group_events(
            group_id=group.id, start=start_dt, end=end_dt
        )
        # Apply the serializer's optimization so nested relations are prefetched
        # (get_group_events returns a real queryset, not synthetic occurrences).
        context = self.get_serializer_context()
        optimized_events = CalendarEventSerializer(context=context).get_optimized_queryset(events)
        return Response(
            CalendarEventSerializer(list(optimized_events), many=True, context=context).data
        )

    @extend_schema(
        summary="Per-slot availability for requested ranges",
        request=CalendarGroupAvailabilityQuerySerializer,
        responses={200: CalendarGroupRangeAvailabilitySerializer(many=True)},
    )
    @action(
        methods=["POST"],
        detail=True,
        url_path="availability",
        url_name="availability",
    )
    @inject
    def availability(
        self,
        request,
        pk,
        calendar_group_service: Annotated[CalendarGroupService, Provide["calendar_group_service"]],
    ):
        group = self.get_object()
        input_serializer = CalendarGroupAvailabilityQuerySerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)

        calendar_group_service.initialize(organization=group.organization)
        ranges = [
            (r["start_time"], r["end_time"]) for r in input_serializer.validated_data["ranges"]
        ]
        result = calendar_group_service.check_group_availability(group_id=group.id, ranges=ranges)
        payload = [
            {
                "start_time": r.start_time,
                "end_time": r.end_time,
                "slots": [
                    {
                        "slot_id": s.slot_id,
                        "available_calendar_ids": s.available_calendar_ids,
                        "required_count": s.required_count,
                    }
                    for s in r.slots
                ],
            }
            for r in result
        ]
        return Response(CalendarGroupRangeAvailabilitySerializer(payload, many=True).data)

    @extend_schema(
        summary="Bookable slot proposals for the group within a search window",
        parameters=[
            OpenApiParameter(
                name="search_window_start",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Start of the search window (ISO 8601)",
                required=True,
            ),
            OpenApiParameter(
                name="search_window_end",
                type=str,
                location=OpenApiParameter.QUERY,
                description="End of the search window (ISO 8601)",
                required=True,
            ),
            OpenApiParameter(
                name="duration_seconds",
                type=int,
                location=OpenApiParameter.QUERY,
                description="Desired event duration, in seconds",
                required=True,
            ),
            OpenApiParameter(
                name="slot_step_seconds",
                type=int,
                location=OpenApiParameter.QUERY,
                description="Search step, in seconds (default 900 = 15min)",
                required=False,
            ),
        ],
        responses={200: BookableSlotProposalSerializer(many=True)},
    )
    @action(
        methods=["GET"],
        detail=True,
        url_path="bookable-slots",
        url_name="bookable-slots",
    )
    @inject
    def bookable_slots(
        self,
        request,
        pk,
        calendar_group_service: Annotated[CalendarGroupService, Provide["calendar_group_service"]],
    ):
        group = self.get_object()
        try:
            start_dt = datetime.datetime.fromisoformat(
                request.query_params["search_window_start"].replace("Z", "+00:00")
            )
            end_dt = datetime.datetime.fromisoformat(
                request.query_params["search_window_end"].replace("Z", "+00:00")
            )
            duration_seconds = int(request.query_params["duration_seconds"])
            slot_step_seconds = int(request.query_params.get("slot_step_seconds", 15 * 60))
        except (KeyError, ValueError) as e:
            raise ValidationError(
                {
                    "non_field_errors": [
                        "search_window_start, search_window_end and duration_seconds are required "
                        "ISO/integer values."
                    ]
                }
            ) from e

        calendar_group_service.initialize(organization=group.organization)
        try:
            proposals = calendar_group_service.find_bookable_slots(
                group_id=group.id,
                search_window_start=start_dt,
                search_window_end=end_dt,
                duration=datetime.timedelta(seconds=duration_seconds),
                slot_step=datetime.timedelta(seconds=slot_step_seconds),
            )
        except CalendarGroupError as e:
            raise ValidationError({"non_field_errors": [str(e)]}) from e

        payload = [{"start_time": p.start_time, "end_time": p.end_time} for p in proposals]
        return Response(BookableSlotProposalSerializer(payload, many=True).data)


@extend_schema(tags=["Booking Policies"])
class BookingPolicyViewSet(VintaScheduleModelViewSet):
    """ViewSet for managing ``BookingPolicy`` objects.

    Provides full CRUD for booking policies scoped to the authenticated user's
    organization.  Write operations (create, update, delete) delegate to
    ``BookingPolicyService`` so validation, uniqueness checking, and audit
    emission live in a single place.

    **Exactly-one-target rule:** create requests must supply exactly one of
    ``calendar``, ``membership_user_id``, ``calendar_group``, or
    ``is_organization_default=true``.  Any other combination returns 400.

    **Duplicate target → 400:** creating a second policy for the same target
    is a validation error (not a 409) so the serializer can name the conflict.

    **Idempotent destroy:** ``DELETE /booking-policies/{id}/`` returns 204 even
    when the policy does not exist for the bound organization.
    """

    permission_classes = (BookingPolicyPermission,)
    queryset = BookingPolicy.objects.all()
    serializer_class = BookingPolicySerializer

    def get_queryset(self):
        """Return all policies for the authenticated user's organization."""
        user = self.request.user
        if not user.is_authenticated:
            return BookingPolicy.original_manager.none()

        membership = get_active_organization_membership(user)
        if not membership:
            return BookingPolicy.original_manager.none()

        return BookingPolicy.objects.filter_by_organization(membership.organization_id)

    def _build_service(
        self,
        booking_policy_service: "BookingPolicyService",
    ) -> "BookingPolicyService":
        """Initialize the service for this request's organization + actor."""
        membership = get_active_organization_membership(cast("Any", self.request.user))
        if membership is None:
            # Callers without a membership are gated at the permission layer;
            # this branch is a safeguard only.
            return booking_policy_service

        booking_policy_service.initialize(membership.organization)

        # Resolve the acting principal for audit records.
        actor = AuditService.actor_from_user(self.request.user, membership.organization_id)
        booking_policy_service.set_actor(actor)

        return booking_policy_service

    def get_serializer_context(self):
        """Inject the initialized ``BookingPolicyService`` into the serializer context."""
        context = super().get_serializer_context()
        # We defer service construction to the @inject-decorated action methods
        # (create / update / destroy) so DI is wired correctly per-request.
        # The context key is set there, not here, to keep get_serializer_context pure.
        return context

    @extend_schema(
        summary="Create a booking policy",
        description=(
            "Create a new booking policy for the organization. "
            "Exactly one of 'calendar', 'membership_user_id', 'calendar_group', "
            "or 'is_organization_default' must be set. "
            "Returns 400 when a policy for the target already exists."
        ),
        responses={201: BookingPolicySerializer},
    )
    @inject
    def create(
        self,
        request,
        *args,
        booking_policy_service: Annotated[
            BookingPolicyService, Provide["booking_policy_service"]
        ] = None,  # type: ignore[assignment]
        **kwargs,
    ) -> Response:
        """POST /booking-policies/ — create a new policy."""
        service = self._build_service(booking_policy_service)

        serializer = self.get_serializer(data=request.data)
        # Pass the initialized service through context so BookingPolicySerializer
        # can delegate to it without direct-importing the service.
        cast(dict, serializer.context)["booking_policy_service"] = service
        serializer.is_valid(raise_exception=True)
        policy = serializer.save()

        # Re-fetch through the queryset so annotations are applied uniformly.
        policy = self.get_queryset().get(pk=policy.pk)
        return Response(self.get_serializer(policy).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="Update a booking policy",
        description=(
            "Update the rule fields (lead_time_seconds, max_horizon_seconds, "
            "buffer_before_seconds, buffer_after_seconds) of an existing booking policy. "
            "Target fields (calendar, membership_user_id, calendar_group, "
            "is_organization_default) are immutable after creation."
        ),
        responses={200: BookingPolicySerializer},
    )
    @inject
    def update(
        self,
        request,
        *args,
        booking_policy_service: Annotated[
            BookingPolicyService, Provide["booking_policy_service"]
        ] = None,  # type: ignore[assignment]
        **kwargs,
    ) -> Response:
        """PUT/PATCH /booking-policies/{id}/ — update rule fields."""
        service = self._build_service(booking_policy_service)

        partial = kwargs.pop("partial", False)
        instance = self.get_object()

        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        cast(dict, serializer.context)["booking_policy_service"] = service
        serializer.is_valid(raise_exception=True)
        policy = serializer.save()

        # Re-fetch through the queryset to ensure consistency with the list/retrieve paths.
        policy = self.get_queryset().get(pk=policy.pk)
        return Response(self.get_serializer(policy).data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Delete a booking policy",
        description=(
            "Delete a booking policy by id. "
            "Returns 204 even when the policy does not exist (idempotent no-op)."
        ),
        responses={204: None},
    )
    @inject
    def destroy(
        self,
        request,
        *args,
        booking_policy_service: Annotated[
            BookingPolicyService, Provide["booking_policy_service"]
        ] = None,  # type: ignore[assignment]
        **kwargs,
    ) -> Response:
        """DELETE /booking-policies/{id}/ — idempotent policy removal.

        Returns 204 regardless of whether the policy existed.  The plan's
        Guiding Decisions mandate delete-absent semantics: a missing row is not
        an error on the caller's side.
        """
        service = self._build_service(booking_policy_service)

        # Try to resolve the policy but do not 404 when absent — the contract
        # is idempotent no-op.
        membership = get_active_organization_membership(request.user)
        if membership is None:
            # Gated — permission layer should catch this first.
            return Response(status=status.HTTP_204_NO_CONTENT)

        pk = kwargs.get("pk") or self.kwargs.get("pk")
        try:
            policy: BookingPolicy | None = BookingPolicy.objects.filter_by_organization(
                membership.organization_id
            ).get(pk=pk)
        except BookingPolicy.DoesNotExist:
            policy = None

        service.delete_booking_policy(policy)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=["External Event Change Requests"])
class ExternalEventChangeRequestViewSet(ReadOnlyVintaScheduleModelViewSet):
    """List and act on external-event change requests (approve / reject).

    **Eligibility scoping (GET /change-requests/):**
    - **Admins** see all change requests in their organization.
    - **Members** see only requests whose target event they attend
      (``EventAttendance`` row for their membership).

    **Default filter:** ``status=PENDING``.  Pass ``?status=approved`` (or any
    valid status) to retrieve historical / resolved requests.

    **Approve (POST /change-requests/{id}/approve/):**
    Apply the proposed change locally.  Returns the updated request (200).

    **Reject (POST /change-requests/{id}/reject/):**
    Push the retained value back to the external provider (GCal) and mark
    the request ``REJECTED``.  Returns the updated request (200).

    **Error responses:**
    - ``403`` when the caller is not eligible to resolve the specific request.
    - ``409`` when the request is no longer ``PENDING``.
    - ``401`` when the caller is not authenticated.
    """

    permission_classes = (ExternalEventChangeRequestPermission,)
    queryset = ExternalEventChangeRequest.objects.all()
    serializer_class = ExternalEventChangeRequestSerializer
    filterset_class = ExternalEventChangeRequestFilterSet
    http_method_names = ("get", "post", "head", "options")

    def get_queryset(self):
        """Return change requests the authenticated membership is eligible to resolve.

        Defaults to ``PENDING`` requests when no ``status`` filter is passed in the
        query string (detected by checking ``self.request.query_params``).  The
        filterset later narrows by ``?status=...`` or ``?event=...`` when provided.
        """
        membership = get_active_organization_membership(self.request.user)
        if not membership:
            return ExternalEventChangeRequest.original_manager.none()

        qs = ExternalEventChangeRequest.objects.filter_by_organization(
            membership.organization_id
        ).resolvable_by(membership)

        # Default to PENDING only on the list action.  Detail actions (approve,
        # reject) must find the request regardless of status so the service can
        # raise ChangeRequestNotPendingError (→ 409) rather than returning 404.
        if self.action == "list" and "status" not in self.request.query_params:
            qs = qs.filter(status=ExternalEventChangeRequestStatus.PENDING)

        return qs

    @extend_schema(
        summary="Approve a change request",
        description=(
            "Apply the proposed change locally and mark the request APPROVED. "
            "Returns 403 when the caller is not eligible to resolve this request; "
            "409 when the request is no longer PENDING."
        ),
        request=None,
        responses={
            200: ExternalEventChangeRequestSerializer,
            403: OpenApiResponse(description="Caller is not eligible to resolve this request."),
            409: OpenApiResponse(description="Request is no longer PENDING."),
        },
    )
    @action(detail=True, methods=["post"], url_path="approve", url_name="approve")
    @inject
    def approve(
        self,
        request,
        pk: str | None = None,
        change_request_service: Annotated[
            ExternalEventChangeRequestService, Provide["external_event_change_request_service"]
        ] = None,  # type: ignore[assignment]
    ) -> Response:
        """POST /change-requests/{id}/approve/ — apply the change locally."""
        change_request = self.get_object()
        membership = get_active_organization_membership(request.user)
        if not membership:
            return Response(
                {"detail": "User is not an active member of any organization."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            updated = change_request_service.approve(change_request, membership=membership)
        except ChangeRequestIneligibleError as exc:
            raise PermissionDenied(str(exc)) from exc
        except ChangeRequestNotPendingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)

        serializer = self.get_serializer(updated)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Reject a change request",
        description=(
            "Push the retained value back to the external provider and mark the request REJECTED. "
            "Requires a valid social account for the event's calendar provider. "
            "Returns 403 when the caller is not eligible to resolve this request; "
            "409 when the request is no longer PENDING."
        ),
        request=None,
        responses={
            200: ExternalEventChangeRequestSerializer,
            400: OpenApiResponse(
                description="No social account for the calendar's provider or no calendar owner."
            ),
            403: OpenApiResponse(description="Caller is not eligible to resolve this request."),
            409: OpenApiResponse(description="Request is no longer PENDING."),
        },
    )
    @action(detail=True, methods=["post"], url_path="reject", url_name="reject")
    @inject
    def reject(
        self,
        request,
        pk: str | None = None,
        calendar_service: Annotated[CalendarService, Provide["calendar_service"]] = None,  # type: ignore[assignment]
        change_request_service: Annotated[
            ExternalEventChangeRequestService, Provide["external_event_change_request_service"]
        ] = None,  # type: ignore[assignment]
    ) -> Response:
        """POST /change-requests/{id}/reject/ — outbound undo on the provider."""
        change_request = self.get_object()
        membership = get_active_organization_membership(request.user)
        if not membership:
            return Response(
                {"detail": "User is not an active member of any organization."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Guard 1: non-PENDING → 409 immediately, before any outbound-auth work.
        if change_request.status != ExternalEventChangeRequestStatus.PENDING:
            return Response(
                {"detail": "Change request is no longer pending."},
                status=status.HTTP_409_CONFLICT,
            )

        # Guard 2: event was deleted → ineligible to reject (403, not 409).
        event = change_request.event
        if event is None:
            return Response(
                {"detail": "Cannot reject a change request with no associated event."},
                status=status.HTTP_403_FORBIDDEN,
            )

        calendar = event.calendar
        if calendar is None:
            return Response(
                {"detail": "Event has no associated calendar; cannot authenticate provider."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Guard 3+: event present + PENDING — resolve calendar owner and social account.
        # Authenticate the CalendarService using the calendar's owner's credentials
        # (matching the pattern in ``CalendarEventViewSet.transfer`` and
        # ``CalendarViewSet.admin_sync``).  Use the owner of the event's calendar
        # rather than the requester's credentials, because the requester may not own
        # the calendar (e.g. an admin approving on behalf of a team member).
        ownership = (
            CalendarOwnership.objects.filter(
                calendar=calendar,
                organization_id=calendar.organization_id,
                membership_user_id__isnull=False,
            )
            .order_by("-is_default", "id")
            .first()
        )

        if not ownership:
            return Response(
                {"detail": "Calendar has no owner; cannot authenticate with provider."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        owner_social_account = SocialAccount.objects.filter(
            user_id=ownership.membership_user_id, provider=calendar.provider
        ).first()

        if not owner_social_account:
            return Response(
                {
                    "detail": (
                        f"Calendar owner has no linked {calendar.provider} account; "
                        "cannot push the undo to the provider."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Authenticate the service and resolve the write adapter for the event's calendar.
        calendar_service.authenticate(
            account=owner_social_account,
            organization=membership.organization,
        )
        write_adapter = calendar_service._get_write_adapter_for_calendar(calendar)

        if write_adapter is None:
            return Response(
                {"detail": "Could not resolve a write adapter for the calendar's provider."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            updated = change_request_service.reject(
                change_request, membership=membership, write_adapter=write_adapter
            )
        except ChangeRequestIneligibleError as exc:
            raise PermissionDenied(str(exc)) from exc
        except ChangeRequestNotPendingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)

        serializer = self.get_serializer(updated)
        return Response(serializer.data, status=status.HTTP_200_OK)
