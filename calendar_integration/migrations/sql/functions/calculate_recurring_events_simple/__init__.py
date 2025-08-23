from common.raw_sql_migration_managers import FunctionMigrationManager


class CalculateRecurringEventsSimpleMigrationManager(FunctionMigrationManager):
    name = "calculate_recurring_events_simple"


__all__ = [
    "CalculateRecurringEventsSimpleMigrationManager"
]