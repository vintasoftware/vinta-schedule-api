from common.raw_sql_migration_managers import FunctionMigrationManager


class CalculateRecurringBlockedTimesWithBulkModificationsMigrationManager(FunctionMigrationManager):
    name = "calculate_recurring_blocked_times_with_bulk_modifications"


__all__ = [
    "CalculateRecurringBlockedTimesWithBulkModificationsMigrationManager"
]
