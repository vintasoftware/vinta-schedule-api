from common.raw_sql_migration_managers import FunctionMigrationManager


class ManagedCalendarIsFreeInRangeMigrationManager(FunctionMigrationManager):
    name = "managed_calendar_is_free_in_range"


__all__ = ["ManagedCalendarIsFreeInRangeMigrationManager"]
