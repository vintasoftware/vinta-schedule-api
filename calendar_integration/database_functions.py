from django.contrib.postgres.fields import ArrayField
from django.db.models import Func, JSONField


class GetEventOccurrencesJSON(Func):
    """
    Custom Django database function to get event occurrences as JSON array.

    Usage:
        from calendar_integration.database_functions import GetEventOccurrencesJSON

        # Annotate events with their occurrences in a date range
        events = CalendarEvent.objects.annotate(
            occurrences=GetEventOccurrencesJSON('id', start_date, end_date, max_occurrences)
        )

        # Filter events that have occurrences in the range
        events_with_occurrences = events.exclude(occurrences=[])

        # Access the occurrences (no JSON parsing needed!)
        for event in events:
            occurrences = event.occurrences  # Already a list of dictionaries
            for occ in occurrences:
                print(f"Occurrence: {occ['start_time']} - {occ['end_time']}")
    """

    function = "get_event_occurrences_json"
    output_field = ArrayField(JSONField())  # PostgreSQL function returns TEXT[] with JSON strings


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
    """
    Custom Django database function to get blocked time occurrences as JSON array.

    Usage:
        from calendar_integration.database_functions import GetBlockedTimeOccurrencesJSON

        # Annotate blocked times with their occurrences in a date range
        blocked_times = BlockedTime.objects.annotate(
            occurrences=GetBlockedTimeOccurrencesJSON('id', start_date, end_date, max_occurrences)
        )

        # Filter blocked times that have occurrences in the range
        blocked_times_with_occurrences = blocked_times.exclude(occurrences=[])

        # Access the occurrences (no JSON parsing needed!)
        for blocked_time in blocked_times:
            occurrences = blocked_time.occurrences  # Already a list of dictionaries
            for occ in occurrences:
                print(f"Blocked Time Occurrence: {occ['start_time']} - {occ['end_time']}")
    """

    function = "get_blocked_time_occurrences_json"
    output_field = ArrayField(JSONField())  # PostgreSQL function returns TEXT[] with JSON strings


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
    """
    Custom Django database function to get available time occurrences as JSON array.

    Usage:
        from calendar_integration.database_functions import GetAvailableTimeOccurrencesJSON

        # Annotate available times with their occurrences in a date range
        available_times = AvailableTime.objects.annotate(
            occurrences=GetAvailableTimeOccurrencesJSON('id', start_date, end_date, max_occurrences)
        )

        # Filter available times that have occurrences in the range
        available_times_with_occurrences = available_times.exclude(occurrences=[])

        # Access the occurrences (no JSON parsing needed!)
        for available_time in available_times:
            occurrences = available_time.occurrences  # Already a list of dictionaries
            for occ in occurrences:
                print(f"Available Time Occurrence: {occ['start_time']} - {occ['end_time']}")
    """

    function = "get_available_time_occurrences_json"
    output_field = ArrayField(JSONField())  # PostgreSQL function returns TEXT[] with JSON strings


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
