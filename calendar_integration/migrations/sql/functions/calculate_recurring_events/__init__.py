from common.raw_sql_migration_managers import FunctionMigrationManager


class CalculateRecurringEventsMigrationManager(FunctionMigrationManager):
    name = "calculate_recurring_events"


__all__ = [
    "CalculateRecurringEventsMigrationManager"
]