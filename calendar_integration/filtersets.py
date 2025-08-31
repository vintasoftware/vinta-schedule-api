from django_filters import rest_framework as filters

from calendar_integration.models import AvailableTime, BlockedTime, Calendar, CalendarEvent


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

        self.filters["calendar"] = filters.ModelChoiceFilter(
            field_name="calendar_fk_id",
            label="Filter by calendar ID",
            queryset=(
                Calendar.objects.filter_by_organization(
                    self.request.user.organization_membership.organization_id
                )
                if self.request.user and self.request.user.is_authenticated
                else Calendar.objects.none()
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

        self.filters["calendar"] = filters.ModelChoiceFilter(
            field_name="calendar_fk_id",
            label="Filter by calendar ID",
            queryset=(
                Calendar.objects.filter_by_organization(
                    self.request.user.organization_membership.organization_id
                )
                if self.request.user and self.request.user.is_authenticated
                else Calendar.objects.none()
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

        self.filters["calendar"] = filters.ModelChoiceFilter(
            field_name="calendar_fk_id",
            label="Filter by calendar ID",
            queryset=(
                Calendar.objects.filter_by_organization(
                    self.request.user.organization_membership.organization_id
                )
                if self.request.user and self.request.user.is_authenticated
                else Calendar.objects.none()
            ),
        )
