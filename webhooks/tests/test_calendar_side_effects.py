import datetime
from unittest.mock import MagicMock

import pytest
from model_bakery import baker

from calendar_integration.services.calendar_side_effects_service import (
    CalendarSideEffectsService,
    OnUpdateAttendeeOnEventHandler,
)
from calendar_integration.services.dataclasses import CalendarEventData, EventExternalAttendeeData
from organizations.models import Organization
from webhooks.constants import WebhookEventType
from webhooks.services.webhook_calendar_side_effects import WebhookCalendarEventSideEffectsService


class TestWebhookCalendarEventSideEffectsServiceSatisfiesProtocol:
    """This is the important check here: it catches the dispatch bug where the
    handler's method name did not match the ``OnUpdateAttendeeOnEventHandler``
    Protocol, so ``isinstance`` silently failed and the webhook never fired.
    """

    def test_satisfies_on_update_attendee_on_event_handler_protocol(self):
        assert issubclass(WebhookCalendarEventSideEffectsService, OnUpdateAttendeeOnEventHandler)


@pytest.mark.django_db
class TestWebhookCalendarEventSideEffectsServiceDispatch:
    @pytest.fixture
    def organization(self):
        return baker.make(Organization, name="Test Org")

    @pytest.fixture
    def mock_webhook_service(self):
        mock = MagicMock()
        mock.send_event.return_value = []
        return mock

    @pytest.fixture
    def handler(self, mock_webhook_service):
        return WebhookCalendarEventSideEffectsService(webhook_service=mock_webhook_service)

    @pytest.fixture
    def side_effects_service(self, handler):
        return CalendarSideEffectsService(side_effects_pipeline=(handler,))

    @pytest.fixture
    def event_data(self):
        return CalendarEventData(
            id=1,
            calendar_id=2,
            start_time=datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.UTC),
            timezone="UTC",
            title="Sprint Review",
            description="",
            external_id="ext-1",
            calendar_settings=None,
            status="confirmed",
            attendees=[],
            external_attendees=[],
            resources=[],
            recurrence_rule=None,
            is_recurring=False,
            recurring_event_id=None,
        )

    @pytest.fixture
    def attendance(self):
        return EventExternalAttendeeData(
            email="attendee@example.com", name="Attendee", status="accepted"
        )

    def test_on_update_attendee_on_event_sends_attendee_updated_webhook(
        self, side_effects_service, mock_webhook_service, organization, event_data, attendance
    ):
        """Driving the pipeline's on_update_attendee_on_event must send
        CALENDAR_EVENT_ATTENDEE_UPDATED through the webhook service."""
        side_effects_service.on_update_attendee_on_event(
            actor=None,
            event=event_data,
            attendee=attendance,
            organization=organization,
        )

        mock_webhook_service.send_event.assert_called_once()
        call_kwargs = mock_webhook_service.send_event.call_args[1]
        assert call_kwargs["event_type"] == WebhookEventType.CALENDAR_EVENT_ATTENDEE_UPDATED
        assert call_kwargs["organization"] == organization
