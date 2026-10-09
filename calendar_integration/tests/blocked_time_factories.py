"""Builders for blocked-time tests."""

import datetime

from calendar_integration.constants import BlockedTimeDeclineScope, ProviderPushStatus
from calendar_integration.models import BlockedTime, Calendar


def make_pending_block(calendar: Calendar, **fields) -> BlockedTime:
    """Build a 2026-10-09 10:00-12:00 São Paulo block with options, waiting to be pushed."""
    values = {
        "calendar_fk": calendar,
        "organization": calendar.organization,
        "start_time_tz_unaware": datetime.datetime(2026, 10, 9, 10, 0),
        "end_time_tz_unaware": datetime.datetime(2026, 10, 9, 12, 0),
        "timezone": "America/Sao_Paulo",
        "reason": "Conference",
        "external_id": "manual-2026-10-09T10:00:00-0",
        "decline_scope": BlockedTimeDeclineScope.NEW,
        "decline_message": "Back on Monday.",
        "provider_push_status": ProviderPushStatus.PENDING,
    }
    values.update(fields)
    return BlockedTime.objects.create(**values)
