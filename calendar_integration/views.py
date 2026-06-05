import datetime
from collections.abc import Callable
from typing import Annotated

from django.db import transaction
from django.http import Http404

from allauth.socialaccount.models import SocialAccount
from dependency_injector.wiring import Provide, inject
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from calendar_integration.exceptions import (
    CalendarGroupError,
    CalendarIntegrationError,
)
from calendar_integration.filtersets import (
    AvailableTimeFilterSet,
    BlockedTimeFilterSet,
    CalendarEventFilterSet,
    CalendarGroupFilterSet,
)
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarGroup,
    CalendarOwnership,
)
from calendar_integration.permissions import (
    CalendarAvailabilityPermission,
    CalendarEventPermission,
    CalendarGroupPermission,
)
from calendar_integration.serializers import (
    AvailableTimeBulkModificationSerializer,
    AvailableTimeRecurringExceptionSerializer,
    AvailableTimeSerializer,
    AvailableTimeWindowSerializer,
    BlockedTimeBulkModificationSerializer,
    BlockedTimeRecurringExceptionSerializer,
    BlockedTimeSerializer,
    BookableSlotProposalSerializer,
    BulkAvailableTimeSerializer,
    BulkBlockedTimeSerializer,
    CalendarBundleCreateSerializer,
    CalendarEventSerializer,
    CalendarGroupAvailabilityQuerySerializer,
    CalendarGroupEventCreateSerializer,
    CalendarGroupRangeAvailabilitySerializer,
    CalendarGroupSerializer,
    CalendarSerializer,
    CalendarSyncRequestSerializer,
    CalendarSyncSerializer,
    EventBulkModificationSerializer,
    EventRecurringExceptionSerializer,
    UnavailableTimeWindowSerializer,
)
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.calendar_service import CalendarService
from common.utils.view_utils import VintaScheduleModelViewSet
from organizations.models import get_active_organization_membership


class CalendarViewSet(VintaScheduleModelViewSet):
    """
    ViewSet for managing calendars.
    """

    permission_classes = (CalendarAvailabilityPermission,)
    queryset = Calendar.objects.all()
    serializer_class = CalendarSerializer

    def get_queryset(self):
        """Filter calendars by user's accessible calendar organizations."""
        user = self.request.user
        if not user.is_authenticated:
            return Calendar.original_manager.none()

        membership = get_active_organization_membership(user)
        if not membership:
            # Membership-less or inactive members get an empty queryset, not a 500.
            return Calendar.original_manager.none()
        return super().get_queryset().filter_by_organization(membership.organization_id)

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

        social_accounts = SocialAccount.objects.filter(user=user)
        if not social_accounts.exists():
            return Response(
                {"detail": "User has no connected external calendar account."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            for social_account in social_accounts:
                fresh_service = calendar_service_factory()

                fresh_service.authenticate(
                    account=social_account,
                    organization=membership.organization,
                )

                def enqueue_import(service=fresh_service):
                    service.request_calendars_import()

                transaction.on_commit(enqueue_import)

            account_count = social_accounts.count()
            return Response(
                {
                    "detail": f"Calendar import requested for {account_count} account(s)."
                    if account_count > 1
                    else "Calendar import requested."
                },
                status=status.HTTP_202_ACCEPTED,
            )
        except (ValueError, CalendarIntegrationError) as e:
            raise ValidationError({"non_field_errors": [str(e)]}) from e

    @extend_schema(
        summary="Request calendar sync",
        description="Request synchronization of an owned calendar over a date range.",
        request=CalendarSyncRequestSerializer,
        responses={202: CalendarSyncSerializer()},
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
            user=user,
            organization_id=calendar.organization_id,
        ).exists():
            return Response(
                {"detail": "You do not own this calendar."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Parse and validate datetime fields
        start_datetime_str = request.data.get("start_datetime")
        end_datetime_str = request.data.get("end_datetime")

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
            raise ValidationError({"non_field_errors": [f"Invalid datetime format: {e!s}"]}) from e

        should_update_events = request.data.get("should_update_events", False)

        # Get social account for authentication
        social_account = SocialAccount.objects.filter(user=user, provider=calendar.provider).first()

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

        return BlockedTime.objects.filter_by_organization(membership.organization.id)

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

        return Response(
            BlockedTimeSerializer(
                blocked_times, many=True, context=self.get_serializer_context()
            ).data,
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

        return AvailableTime.objects.filter_by_organization(membership.organization.id)

    @extend_schema(
        summary="Create bulk available times",
        request=BulkAvailableTimeSerializer,
        responses={201: AvailableTimeSerializer(many=True)},
    )
    @action(
        methods=["POST"],
        detail=False,
        url_path="bulk-create",
        url_name="bulk-create",
    )
    def bulk_create(self, request):
        """Create multiple available times."""
        serializer = BulkAvailableTimeSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        available_times = serializer.save()

        return Response(
            AvailableTimeSerializer(
                available_times, many=True, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_201_CREATED,
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
        return Response(
            CalendarEventSerializer(event, context=self.get_serializer_context()).data,
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
        return Response(
            CalendarEventSerializer(
                list(events), many=True, context=self.get_serializer_context()
            ).data
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
