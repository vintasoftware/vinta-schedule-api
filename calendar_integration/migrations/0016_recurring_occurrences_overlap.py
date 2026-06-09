from django.db import migrations

from calendar_integration.migrations.sql.functions.calculate_recurring_available_times import (
    CalculateRecurringAvailableTimesMigrationManager,
)
from calendar_integration.migrations.sql.functions.calculate_recurring_blocked_times import (
    CalculateRecurringBlockedTimesMigrationManager,
)
from calendar_integration.migrations.sql.functions.calculate_recurring_events import (
    CalculateRecurringEventsMigrationManager,
)
from calendar_integration.migrations.sql.functions.get_available_time_occurrences_json import (
    GetAvailableTimeOccurrencesJSONMigrationManager,
)
from calendar_integration.migrations.sql.functions.get_blocked_time_occurrences_json import (
    GetBlockedTimeOccurrencesJSONMigrationManager,
)
from calendar_integration.migrations.sql.functions.get_event_occurrences_json import (
    GetEventOccurrencesJsonMigrationManager,
)


class Migration(migrations.Migration):
    dependencies = [
        ("calendar_integration", "0015_calendarsync_trigger_source"),
    ]

    operations = [
        # Base generators first (wrappers call them)
        CalculateRecurringAvailableTimesMigrationManager(
            app_path="calendar_integration",
            version="0002",
        ).migration(),
        CalculateRecurringBlockedTimesMigrationManager(
            app_path="calendar_integration",
            version="0002",
        ).migration(),
        CalculateRecurringEventsMigrationManager(
            app_path="calendar_integration",
            version="0002",
        ).migration(),
        # Wrappers after (they call the base functions)
        GetAvailableTimeOccurrencesJSONMigrationManager(
            app_path="calendar_integration",
            version="0002",
        ).migration(),
        GetBlockedTimeOccurrencesJSONMigrationManager(
            app_path="calendar_integration",
            version="0002",
        ).migration(),
        GetEventOccurrencesJsonMigrationManager(
            app_path="calendar_integration",
            version="0002",
        ).migration(),
    ]
