from common.raw_sql_migration_managers import FunctionMigrationManager


class GetBlockedTimeOccurrencesJSONMigrationManager(FunctionMigrationManager):
    name = "get_blocked_time_occurrences_json"


__all__ = [
    "GetBlockedTimeOccurrencesJSONMigrationManager"
]
