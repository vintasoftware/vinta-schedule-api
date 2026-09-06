from common.raw_sql_migration_managers import FunctionMigrationManager


class CalculateAppointmentTypeQuotaPeriodCountsMigrationManager(FunctionMigrationManager):
    name = "calculate_appointment_type_quota_period_counts"


__all__ = ["CalculateAppointmentTypeQuotaPeriodCountsMigrationManager"]
