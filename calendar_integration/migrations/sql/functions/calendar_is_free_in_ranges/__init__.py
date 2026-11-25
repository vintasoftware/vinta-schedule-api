from common.raw_sql_migration_managers import FunctionMigrationManager


class CalendarIsFreeInRangesMigrationManager(FunctionMigrationManager):
    name = "calendar_is_free_in_ranges"


__all__ = ["CalendarIsFreeInRangesMigrationManager"]
