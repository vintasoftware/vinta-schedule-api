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
