from common.raw_sql_migration_managers import FunctionMigrationManager


class CalculateRecurringAvailableTimesWithBulkModificationsMigrationManager(FunctionMigrationManager):
    name = "calculate_recurring_available_times_with_bulk_modifications"


__all__ = [
    "CalculateRecurringAvailableTimesWithBulkModificationsMigrationManager"
]
