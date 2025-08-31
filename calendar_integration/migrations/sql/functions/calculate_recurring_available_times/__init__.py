from common.raw_sql_migration_managers import FunctionMigrationManager


class CalculateRecurringAvailableTimesMigrationManager(FunctionMigrationManager):
    name = "calculate_recurring_available_times"


__all__ = [
    "CalculateRecurringAvailableTimesMigrationManager"
]
