from common.raw_sql_migration_managers import FunctionMigrationManager


class GetBlockedTimeOccurrencesWithBulkModificationsJSONMigrationManager(FunctionMigrationManager):
    name = "get_blocked_time_occurrences_with_bulk_modifications_json"


__all__ = [
    "GetBlockedTimeOccurrencesWithBulkModificationsJSONMigrationManager"
]
