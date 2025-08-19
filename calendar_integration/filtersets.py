from django_filters import rest_framework as filters

from calendar_integration.models import CalendarEvent


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
            "calendar",
        )
