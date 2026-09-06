"""Install the quota-counting functions under their new names, alongside the old ones.

The raw-SQL framework in ``common/raw_sql_migration_managers.py`` takes a
function's name from its directory, so there is no "rename" to express: a new
name is a new directory and a new ``CREATE FUNCTION``. That turns out to be
what we want anyway.

Both functions are dropped and recreated by ``0044``'s reverse, and both are
called from ``calendar_integration/database_functions.py``. Dropping the old
pair in this same migration would leave a window -- between ``migrate``
finishing and the new code rolling out -- where the running release calls a
function that no longer exists. So ``calculate_calendar_group_quota_period_counts``
and ``get_calendar_group_quota_period_counts_json`` are left in place here and
dropped by a follow-up once every environment runs code that asks for the new
names.

They stop resolving once ``0061`` renames the tables underneath them -- both
are ``plpgsql``, so the table names in their bodies are looked up at call time,
not at ``CREATE`` time. That is not a regression to manage but the reason to
keep them: reversing ``0061`` puts the old tables back and makes the old
functions answer again, so the whole rename stays reversible in one step.
"""

from django.db import migrations

from calendar_integration.migrations.sql.functions.calculate_appointment_type_quota_period_counts import (
    CalculateAppointmentTypeQuotaPeriodCountsMigrationManager,
)
from calendar_integration.migrations.sql.functions.get_appointment_type_quota_period_counts_json import (
    GetAppointmentTypeQuotaPeriodCountsJSONMigrationManager,
)


class Migration(migrations.Migration):
    dependencies = [
        ("calendar_integration", "0062_appointmenttype_constraint_and_index_names"),
    ]

    operations = [
        CalculateAppointmentTypeQuotaPeriodCountsMigrationManager(
            "calendar_integration", "0001"
        ).migration(),
        GetAppointmentTypeQuotaPeriodCountsJSONMigrationManager(
            "calendar_integration", "0001"
        ).migration(),
    ]
