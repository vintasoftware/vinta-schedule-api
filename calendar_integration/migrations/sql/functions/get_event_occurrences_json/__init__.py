from common.raw_sql_migration_managers import FunctionMigrationManager


class GetEventOccurrencesJsonMigrationManager(FunctionMigrationManager):
    name = "get_event_occurrences_json"


__all__ = [
    "GetEventOccurrencesJsonMigrationManager"
]