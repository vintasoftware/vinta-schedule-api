"""Public surface for the calendar integration services package.

``CalendarService`` is the DI-injected facade and primary entry point for all
calendar operations.  The sub-services listed below are plain classes constructed
by the facade after authentication; they are exported here for use in tests and
any future consumer that wants to construct them directly (e.g. to unit-test a
sub-service in isolation without going through the facade).

``CalendarServiceContext`` and ``RecurrenceManager`` are auxiliary types that
may be useful in cross-module typing or test fixtures.
"""

from calendar_integration.services.availability_service import AvailabilityService
from calendar_integration.services.calendar_bundle_service import CalendarBundleService
from calendar_integration.services.calendar_event_service import CalendarEventService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.calendar_service_context import CalendarServiceContext
from calendar_integration.services.calendar_sync_service import CalendarSyncService
from calendar_integration.services.calendar_webhook_service import CalendarWebhookService
from calendar_integration.services.recurrence_manager import RecurrenceManager


__all__ = [
    "AvailabilityService",
    "CalendarBundleService",
    "CalendarEventService",
    "CalendarService",
    "CalendarServiceContext",
    "CalendarSyncService",
    "CalendarWebhookService",
    "RecurrenceManager",
]
