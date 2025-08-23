from django.contrib.postgres.fields import ArrayField
from django.db.models import Func, JSONField


class GetEventOccurrencesJSON(Func):
    """
    Custom Django database function to get event occurrences as JSON array.

    Usage:
        from calendar_integration.utils.recurring_events_orm import GetEventOccurrencesJSON

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
