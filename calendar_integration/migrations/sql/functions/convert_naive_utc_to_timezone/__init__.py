from common.raw_sql_migration_managers import FunctionMigrationManager


class ConvertNaiveUtcToTimezoneMigrationManager(FunctionMigrationManager):
    name = "convert_naive_utc_to_timezone"


__all__ = [
    "ConvertNaiveUtcToTimezoneMigrationManager"
]
