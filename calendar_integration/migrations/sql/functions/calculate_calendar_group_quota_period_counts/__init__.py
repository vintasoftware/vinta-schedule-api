from common.raw_sql_migration_managers import FunctionMigrationManager


class CalculateCalendarGroupQuotaPeriodCountsMigrationManager(FunctionMigrationManager):
    name = "calculate_calendar_group_quota_period_counts"


__all__ = ["CalculateCalendarGroupQuotaPeriodCountsMigrationManager"]
