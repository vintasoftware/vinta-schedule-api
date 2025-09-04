from common.raw_sql_migration_managers import FunctionMigrationManager


class CalculateRecurringEventsWithBulkModificationsMigrationManager(FunctionMigrationManager):
    name = "calculate_recurring_events_with_bulk_modifications"


__all__ = [
    "CalculateRecurringEventsWithBulkModificationsMigrationManager"
]