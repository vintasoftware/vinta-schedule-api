from common.raw_sql_migration_managers import FunctionMigrationManager


class TimezoneConversionMigrationManager(FunctionMigrationManager):
    name = "timezone_conversion"


__all__ = [
    "TimezoneConversionMigrationManager"
]
