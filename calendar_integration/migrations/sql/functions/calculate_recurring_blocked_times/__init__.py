from common.raw_sql_migration_managers import FunctionMigrationManager


class CalculateRecurringBlockedTimesMigrationManager(FunctionMigrationManager):
    name = "calculate_recurring_blocked_times"


__all__ = [
    "CalculateRecurringBlockedTimesMigrationManager"
]
