from django_filters import rest_framework as filters

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarGroup,
)
from organizations.models import get_active_organization_membership


class CalendarFilterSet(filters.FilterSet):
    """FilterSet for listing calendars.

    Lets org admins narrow the calendar list to resource calendars, by provider
    (manual ``internal`` vs synced ``google``/``microsoft``/...), and by sync state.
    """

    calendar_type = filters.ChoiceFilter(
        field_name="calendar_type",
        choices=CalendarType.choices,
        label="Filter by calendar type (e.g. resource)",
    )
    provider = filters.ChoiceFilter(
        field_name="provider",
        choices=CalendarProvider.choices,
        label="Filter by provider (internal = manual, others = synced)",
    )
    sync_enabled = filters.BooleanFilter(
        field_name="sync_enabled",
        label="Filter by whether provider sync is enabled",
    )

    class Meta:
        model = Calendar
        fields = (
            "calendar_type",
            "provider",
            "sync_enabled",
        )


class CalendarEventFilterSet(filters.FilterSet):
    """
    FilterSet for CalendarEvent model.
    """

    start_time = filters.DateTimeFilter(
        field_name="start_time",
        lookup_expr="gte",
        label="Start time (greater than or equal to)",
    )
    end_time = filters.DateTimeFilter(
        field_name="end_time",
        lookup_expr="lte",
        label="End time (less than or equal to)",
    )
    start_time_range = filters.DateTimeFromToRangeFilter(
        field_name="start_time",
        label="Start time range",
    )
    end_time_range = filters.DateTimeFromToRangeFilter(
        field_name="end_time",
        label="End time range",
    )
    title = filters.CharFilter(
        field_name="title",
        lookup_expr="icontains",
        label="Filter by partial title match",
    )
    calendar = filters.NumberFilter(
        field_name="calendar_fk_id",
        label="Filter by calendar ID",
    )

    class Meta:
        model = CalendarEvent
        fields = (
            "start_time",
            "end_time",
            "start_time_range",
            "end_time_range",
            "title",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        user = self.request.user if self.request else None
        membership = (
            get_active_organization_membership(user) if user and user.is_authenticated else None
        )
        self.filters["calendar"] = filters.ModelChoiceFilter(
            field_name="calendar_fk_id",
            label="Filter by calendar ID",
            queryset=(
                Calendar.objects.filter_by_organization(membership.organization_id)
                if membership
                else Calendar.original_manager.none()
            ),
        )


class BlockedTimeFilterSet(filters.FilterSet):
    start_time = filters.DateTimeFilter(
        field_name="start_time",
        lookup_expr="gte",
        label="Start time (greater than or equal to)",
    )
    end_time = filters.DateTimeFilter(
        field_name="end_time",
        lookup_expr="lte",
        label="End time (less than or equal to)",
    )
    reason = filters.CharFilter(
        field_name="title",
        lookup_expr="icontains",
        label="Filter by partial title match",
    )

    class Meta:
        model = BlockedTime
        fields = (
            "start_time",
            "end_time",
            "reason",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        user = self.request.user if self.request else None
        membership = (
            get_active_organization_membership(user) if user and user.is_authenticated else None
        )
        self.filters["calendar"] = filters.ModelChoiceFilter(
            field_name="calendar_fk_id",
            label="Filter by calendar ID",
            queryset=(
                Calendar.objects.filter_by_organization(membership.organization_id)
                if membership
                else Calendar.original_manager.none()
            ),
        )


class CalendarGroupFilterSet(filters.FilterSet):
    """FilterSet for CalendarGroup."""

    name = filters.CharFilter(
        field_name="name",
        lookup_expr="icontains",
        label="Filter by partial name match",
    )

    class Meta:
        model = CalendarGroup
        fields = ("name",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        user = self.request.user if self.request else None
        membership = (
            get_active_organization_membership(user) if user and user.is_authenticated else None
        )
        self.filters["calendar"] = filters.ModelChoiceFilter(
            field_name="slots__memberships__calendar_fk_id",
            label="Filter to groups whose slot pools include this calendar",
            queryset=(
                Calendar.objects.filter_by_organization(membership.organization_id)
                if membership
                else Calendar.original_manager.none()
            ),
        )


class AvailableTimeFilterSet(filters.FilterSet):
    start_time = filters.DateTimeFilter(
        field_name="start_time",
        lookup_expr="gte",
        label="Start time (greater than or equal to)",
    )
    end_time = filters.DateTimeFilter(
        field_name="end_time",
        lookup_expr="lte",
        label="End time (less than or equal to)",
    )

    class Meta:
        model = AvailableTime
        fields = (
            "start_time",
            "end_time",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        user = self.request.user if self.request else None
        membership = (
            get_active_organization_membership(user) if user and user.is_authenticated else None
        )
        self.filters["calendar"] = filters.ModelChoiceFilter(
            field_name="calendar_fk_id",
            label="Filter by calendar ID",
            queryset=(
                Calendar.objects.filter_by_organization(membership.organization_id)
                if membership
                else Calendar.original_manager.none()
            ),
        )
