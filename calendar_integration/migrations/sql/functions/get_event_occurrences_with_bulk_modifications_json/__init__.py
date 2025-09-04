from common.raw_sql_migration_managers import FunctionMigrationManager


class GetEventOccurrencesWithBulkModificationsJsonMigrationManager(FunctionMigrationManager):
    name = "get_event_occurrences_with_bulk_modifications_json"


__all__ = [
    "GetEventOccurrencesWithBulkModificationsJsonMigrationManager"
]