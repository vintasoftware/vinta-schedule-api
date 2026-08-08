from django.contrib.postgres.fields import ArrayField
from django.db.models import BooleanField, ExpressionWrapper, Func, JSONField, Value


def _with_overlap(args, overlap: bool):
    """Append a boolean Value for the p_overlap SQL parameter."""
    return (*args, ExpressionWrapper(Value(overlap), output_field=BooleanField()))


class GetEventOccurrencesJSON(Func):
    function = "get_event_occurrences_json"
    output_field = ArrayField(JSONField())

    def __init__(self, *args, overlap: bool = False, **kwargs):
        super().__init__(*_with_overlap(args, overlap), **kwargs)


class GetEventOccurrencesWithBulkModificationsJSON(Func):
    """
    Enhanced Django database function to get event occurrences including bulk modifications as JSON array.

    This function considers both the original recurring event (potentially truncated) and any
    continuation events created by bulk modifications.

    Usage:
        from calendar_integration.database_functions import GetEventOccurrencesWithBulkModificationsJSON

        # Annotate events with their occurrences including bulk modifications
        events = CalendarEvent.objects.annotate(
            occurrences=GetEventOccurrencesWithBulkModificationsJSON('id', start_date, end_date, max_occurrences)
        )

        # Access the occurrences (includes both original and continuation occurrences)
        for event in events:
            occurrences = event.occurrences  # Already a list of dictionaries
            for occ in occurrences:
                is_continuation = occ.get('is_bulk_continuation', False)
                print(f"Occurrence: {occ['start_time']} - {occ['end_time']} (continuation: {is_continuation})")
    """

    function = "get_event_occurrences_with_bulk_modifications_json"
    output_field = ArrayField(JSONField())  # PostgreSQL function returns TEXT[] with JSON strings


class GetBlockedTimeOccurrencesJSON(Func):
    function = "get_blocked_time_occurrences_json"
    output_field = ArrayField(JSONField())

    def __init__(self, *args, overlap: bool = False, **kwargs):
        super().__init__(*_with_overlap(args, overlap), **kwargs)


class GetBlockedTimeOccurrencesWithBulkModificationsJSON(Func):
    """
    Enhanced Django database function to get blocked time occurrences including bulk modifications as JSON array.

    This function considers both the original recurring blocked time (potentially truncated) and any
    continuation blocked times created by bulk modifications.

    Usage:
        from calendar_integration.database_functions import GetBlockedTimeOccurrencesWithBulkModificationsJSON

        # Annotate blocked times with their occurrences including bulk modifications
        blocked_times = BlockedTime.objects.annotate(
            occurrences=GetBlockedTimeOccurrencesWithBulkModificationsJSON('id', start_date, end_date, max_occurrences)
        )

        # Access the occurrences (includes both original and continuation occurrences)
        for blocked_time in blocked_times:
            occurrences = blocked_time.occurrences  # Already a list of dictionaries
            for occ in occurrences:
                is_continuation = occ.get('is_bulk_continuation', False)
                print(f"Blocked Time: {occ['start_time']} - {occ['end_time']} (continuation: {is_continuation})")
    """

    function = "get_blocked_time_occurrences_with_bulk_modifications_json"
    output_field = ArrayField(JSONField())  # PostgreSQL function returns TEXT[] with JSON strings


class GetAvailableTimeOccurrencesJSON(Func):
    function = "get_available_time_occurrences_json"
    output_field = ArrayField(JSONField())

    def __init__(self, *args, overlap: bool = False, **kwargs):
        super().__init__(*_with_overlap(args, overlap), **kwargs)


class GetCalendarGroupQuotaPeriodCountsJSON(Func):
    """
    Database function returning per-period LIVE booking counts for one calendar
    inside one CalendarGroupSlot, as a JSON array (CALENDAR_GROUP_SCOPED_AVAILABILITY
    Phase 3a). Only bookings made THROUGH that group slot (a
    ``CalendarEventGroupSelection`` row for this exact slot+calendar pair) are
    counted; events created directly on the calendar are not. Counts are derived
    on read -- a cancelled booking (its ``CalendarEvent`` row deleted) frees
    quota immediately, and a reschedule moves the count to whichever period its
    new start_time falls into.

    Usage:
        from calendar_integration.database_functions import GetCalendarGroupQuotaPeriodCountsJSON

        Calendar.objects.filter(id__in=calendar_ids).annotate(
            quota_period_counts=GetCalendarGroupQuotaPeriodCountsJSON(
                "id", group_slot_id, organization_id, period_type, week_start,
                range_start, range_end,
            )
        )

        # Access the buckets (already a list of dicts):
        for calendar in calendars:
            for bucket in calendar.quota_period_counts:
                print(bucket["period_start"], bucket["period_end"], bucket["booking_count"])

    ``period_type`` is one of ``calendar_integration.constants.QuotaPeriod``
    ("day" / "week" / "month"); ``week_start`` is one of
    ``organizations.models.WeekStart`` ("monday" / "sunday") and only affects
    week-period bucketing.

    ``period_type``/``week_start`` are always wrapped in ``Value(...)``
    explicitly (never passed through raw): ``Func``'s own argument parsing
    (``Func._parse_expressions``) turns any plain ``str`` positional argument
    into ``F(that_string)`` -- a field reference, not a literal -- which is
    exactly right for ``"id"`` above but would silently misinterpret
    ``"day"``/``"monday"`` as column names instead of the literal values they
    are.
    """

    function = "get_calendar_group_quota_period_counts_json"
    output_field = ArrayField(JSONField())

    def __init__(
        self,
        calendar_id,
        group_slot_id,
        organization_id,
        period_type: str,
        week_start: str,
        range_start,
        range_end,
        **kwargs,
    ):
        super().__init__(
            calendar_id,
            group_slot_id,
            organization_id,
            Value(period_type),
            Value(week_start),
            range_start,
            range_end,
            **kwargs,
        )


class GetAvailableTimeOccurrencesWithBulkModificationsJSON(Func):
    """
    Enhanced Django database function to get available time occurrences including bulk modifications as JSON array.

    This function considers both the original recurring available time (potentially truncated) and any
    continuation available times created by bulk modifications.

    Usage:
        from calendar_integration.database_functions import GetAvailableTimeOccurrencesWithBulkModificationsJSON

        # Annotate available times with their occurrences including bulk modifications
        available_times = AvailableTime.objects.annotate(
            occurrences=GetAvailableTimeOccurrencesWithBulkModificationsJSON('id', start_date, end_date, max_occurrences)
        )

        # Access the occurrences (includes both original and continuation occurrences)
        for available_time in available_times:
            occurrences = available_time.occurrences  # Already a list of dictionaries
            for occ in occurrences:
                is_continuation = occ.get('is_bulk_continuation', False)
                print(f"Available Time: {occ['start_time']} - {occ['end_time']} (continuation: {is_continuation})")
    """

    function = "get_available_time_occurrences_with_bulk_modifications_json"
    output_field = ArrayField(JSONField())  # PostgreSQL function returns TEXT[] with JSON strings
