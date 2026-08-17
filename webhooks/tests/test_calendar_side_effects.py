import datetime
from unittest.mock import MagicMock

import pytest
from dependency_injector import providers
from model_bakery import baker

from calendar_integration.services.calendar_side_effects_service import (
    CalendarSideEffectsService,
    OnAddAttendeeToEventHandler,
    OnCreateEventHandler,
    OnDeleteEventHandler,
    OnRemoveAttendeeFromEventHandler,
    OnUpdateAttendeeOnEventHandler,
    OnUpdateEventHandler,
)
from calendar_integration.services.dataclasses import CalendarEventData, EventExternalAttendeeData
from organizations.models import Organization
from webhooks.constants import WebhookEventType
from webhooks.services import WebhookService
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


class TestContainerWiresCalendarSideEffectsPipeline:
    """The pipeline the DI container builds must hold handler *instances*.

    ``TestWebhookCalendarEventSideEffectsServiceSatisfiesProtocol`` above proves the
    handler class satisfies the Protocols, but that check passes even when the
    container never instantiates the class. That is what happened here: the
    provider was wired as ``side_effects_pipeline=(webhook_calendar_side_effects_service,)``
    -- a plain tuple holding a Provider. ``dependency_injector`` only resolves a
    provider passed as a direct kwarg value, so the pipeline received the
    ``Factory`` object itself. Every ``isinstance(handler, On*Handler)`` guard in
    ``CalendarSideEffectsService`` then returned ``False`` and no calendar event
    webhook of any kind dispatched.

    These tests resolve the real container, so they fail if the wiring regresses to
    any form that leaves providers unresolved. The container is only assigned in
    ``DICoreConfig.ready()``, so they take it via the ``di_container`` fixture
    rather than a module-level import, which would bind ``None`` forever.
    """

    def test_pipeline_contains_instantiated_handlers_not_providers(self, di_container):
        service = di_container.calendar_side_effects_service()

        pipeline = list(service.side_effects_pipeline)

        assert pipeline, "side_effects_pipeline resolved empty"
        for handler in pipeline:
            assert not isinstance(handler, providers.Provider), (
                f"pipeline holds an unresolved provider ({handler!r}); "
                "dependency_injector does not resolve providers nested in a tuple or list "
                "literal -- use providers.List(...)"
            )

    def test_pipeline_handler_satisfies_every_dispatch_protocol(self, di_container):
        service = di_container.calendar_side_effects_service()

        handler = next(iter(service.side_effects_pipeline))

        # CalendarSideEffectsService guards each dispatch method on one of these.
        # A handler failing any check is silently skipped rather than erroring.
        for protocol in (
            OnCreateEventHandler,
            OnUpdateEventHandler,
            OnDeleteEventHandler,
            OnAddAttendeeToEventHandler,
            OnRemoveAttendeeFromEventHandler,
            OnUpdateAttendeeOnEventHandler,
        ):
            assert isinstance(handler, protocol), (
                f"{type(handler).__name__} does not satisfy {protocol.__name__}; "
                "its dispatch would be silently skipped"
            )

    def test_pipeline_handler_has_a_real_webhook_service(self, di_container):
        service = di_container.calendar_side_effects_service()

        handler = next(iter(service.side_effects_pipeline))

        assert isinstance(handler, WebhookCalendarEventSideEffectsService)
        assert isinstance(handler.webhook_service, WebhookService)
