"""Expand recurring events, blocked times and available times in their own timezone.

Version 0003 of each ``calculate_recurring_*`` function switches the session timezone
to the row's IANA zone while it steps through the series, so occurrences keep their
local time across DST changes and BYDAY uses the local weekday. See the header of
each ``0003.sql``. The signatures do not change, so the ``*_with_bulk_modifications``
and ``get_*_occurrences_json`` functions that call these pick up the change as is.
Reversing restores version 0002.

Existing exception rows, and stored UNTIL values, for series in a DST zone were
written under the old UTC stepping. They are not rewritten (the app is development
only), so such rows may stop matching an occurrence after a DST change.
"""

from django.db import migrations

from calendar_integration.migrations.sql.functions.calculate_recurring_available_times import (
    CalculateRecurringAvailableTimesMigrationManager,
)
from calendar_integration.migrations.sql.functions.calculate_recurring_blocked_times import (
    CalculateRecurringBlockedTimesMigrationManager,
)
from calendar_integration.migrations.sql.functions.calculate_recurring_events import (
    CalculateRecurringEventsMigrationManager,
)


class Migration(migrations.Migration):
    dependencies = [
        ("calendar_integration", "0065_managed_calendar_is_free_in_range_function"),
    ]

    operations = [
        CalculateRecurringEventsMigrationManager("calendar_integration", "0003").migration(),
        CalculateRecurringBlockedTimesMigrationManager("calendar_integration", "0003").migration(),
        CalculateRecurringAvailableTimesMigrationManager(
            "calendar_integration", "0003"
        ).migration(),
    ]
