# Generated Django migration for recurring events PostgreSQL functions

from django.db import migrations

from calendar_integration.migrations.sql.functions.calculate_recurring_events import CalculateRecurringEventsMigrationManager
from calendar_integration.migrations.sql.functions.calculate_recurring_events_simple import CalculateRecurringEventsSimpleMigrationManager
from calendar_integration.migrations.sql.functions.get_event_occurrences_json import GetEventOccurrencesJsonMigrationManager


class Migration(migrations.Migration):

    dependencies = [
        ('calendar_integration', '0001_initial'),  # Replace with your latest migration
    ]

    operations = [
        CalculateRecurringEventsMigrationManager("calendar_integration", "0001").migration(),
        CalculateRecurringEventsSimpleMigrationManager("calendar_integration", "0001").migration(),
        GetEventOccurrencesJsonMigrationManager("calendar_integration", "0001").migration(),
    ]
