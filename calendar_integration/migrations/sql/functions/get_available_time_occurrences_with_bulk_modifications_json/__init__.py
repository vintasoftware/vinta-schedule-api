from common.raw_sql_migration_managers import FunctionMigrationManager


class GetAvailableTimeOccurrencesWithBulkModificationsJSONMigrationManager(FunctionMigrationManager):
    name = "get_available_time_occurrences_with_bulk_modifications_json"


__all__ = [
    "GetAvailableTimeOccurrencesWithBulkModificationsJSONMigrationManager"
]
