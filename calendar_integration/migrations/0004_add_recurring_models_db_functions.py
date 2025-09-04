# Generated Django migration for recurring events PostgreSQL functions

from django.db import migrations

from calendar_integration.migrations.sql.functions.calculate_recurring_events import CalculateRecurringEventsMigrationManager
from calendar_integration.migrations.sql.functions.get_event_occurrences_json import GetEventOccurrencesJsonMigrationManager
from calendar_integration.migrations.sql.functions.calculate_recurring_blocked_times import CalculateRecurringBlockedTimesMigrationManager
from calendar_integration.migrations.sql.functions.calculate_recurring_available_times import CalculateRecurringAvailableTimesMigrationManager
from calendar_integration.migrations.sql.functions.get_blocked_time_occurrences_json import GetBlockedTimeOccurrencesJSONMigrationManager
from calendar_integration.migrations.sql.functions.get_available_time_occurrences_json import GetAvailableTimeOccurrencesJSONMigrationManager
from calendar_integration.migrations.sql.functions.calculate_recurring_events_with_bulk_modifications import CalculateRecurringEventsWithBulkModificationsMigrationManager
from calendar_integration.migrations.sql.functions.get_event_occurrences_with_bulk_modifications_json import GetEventOccurrencesWithBulkModificationsJsonMigrationManager
from calendar_integration.migrations.sql.functions.calculate_recurring_blocked_times_with_bulk_modifications import CalculateRecurringBlockedTimesWithBulkModificationsMigrationManager
from calendar_integration.migrations.sql.functions.calculate_recurring_available_times_with_bulk_modifications import CalculateRecurringAvailableTimesWithBulkModificationsMigrationManager
from calendar_integration.migrations.sql.functions.get_blocked_time_occurrences_with_bulk_modifications_json import GetBlockedTimeOccurrencesWithBulkModificationsJSONMigrationManager
from calendar_integration.migrations.sql.functions.get_available_time_occurrences_with_bulk_modifications_json import GetAvailableTimeOccurrencesWithBulkModificationsJSONMigrationManager
from calendar_integration.migrations.sql.functions.timezone_conversion import TimezoneConversionMigrationManager


class Migration(migrations.Migration):

    dependencies = [
        ('calendar_integration', '0003_initial'),  # Replace with your latest migration
    ]

    operations = [
        TimezoneConversionMigrationManager("calendar_integration", "0001").migration(),
        CalculateRecurringEventsMigrationManager("calendar_integration", "0001").migration(),
        GetEventOccurrencesJsonMigrationManager("calendar_integration", "0001").migration(),
        CalculateRecurringBlockedTimesMigrationManager("calendar_integration", "0001").migration(),
        CalculateRecurringAvailableTimesMigrationManager("calendar_integration", "0001").migration(),
        GetBlockedTimeOccurrencesJSONMigrationManager("calendar_integration", "0001").migration(),
        GetAvailableTimeOccurrencesJSONMigrationManager("calendar_integration", "0001").migration(),
        CalculateRecurringEventsWithBulkModificationsMigrationManager("calendar_integration", "0001").migration(),
        GetEventOccurrencesWithBulkModificationsJsonMigrationManager("calendar_integration", "0001").migration(),
        CalculateRecurringBlockedTimesWithBulkModificationsMigrationManager("calendar_integration", "0001").migration(),
        CalculateRecurringAvailableTimesWithBulkModificationsMigrationManager("calendar_integration", "0001").migration(),
        GetBlockedTimeOccurrencesWithBulkModificationsJSONMigrationManager("calendar_integration", "0001").migration(),
        GetAvailableTimeOccurrencesWithBulkModificationsJSONMigrationManager("calendar_integration", "0001").migration(),
    ]
