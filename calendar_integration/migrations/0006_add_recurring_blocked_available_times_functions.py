# Generated Django migration for recurring blocked times and available times PostgreSQL functions

from django.db import migrations

from calendar_integration.migrations.sql.functions.calculate_recurring_blocked_times import CalculateRecurringBlockedTimesMigrationManager
from calendar_integration.migrations.sql.functions.calculate_recurring_available_times import CalculateRecurringAvailableTimesMigrationManager
from calendar_integration.migrations.sql.functions.get_blocked_time_occurrences_json import GetBlockedTimeOccurrencesJSONMigrationManager
from calendar_integration.migrations.sql.functions.get_available_time_occurrences_json import GetAvailableTimeOccurrencesJSONMigrationManager


class Migration(migrations.Migration):

    dependencies = [
        ('calendar_integration', '0005_remove_calendarevent_parent_event_and_more'),
    ]

    operations = [
        CalculateRecurringBlockedTimesMigrationManager("calendar_integration", "0001").migration(),
        CalculateRecurringAvailableTimesMigrationManager("calendar_integration", "0001").migration(),
        GetBlockedTimeOccurrencesJSONMigrationManager("calendar_integration", "0001").migration(),
        GetAvailableTimeOccurrencesJSONMigrationManager("calendar_integration", "0001").migration(),
    ]
