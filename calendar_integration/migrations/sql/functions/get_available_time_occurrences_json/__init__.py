from common.raw_sql_migration_managers import FunctionMigrationManager


class GetAvailableTimeOccurrencesJSONMigrationManager(FunctionMigrationManager):
    name = "get_available_time_occurrences_json"


__all__ = [
    "GetAvailableTimeOccurrencesJSONMigrationManager"
]
