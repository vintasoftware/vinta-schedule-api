"""Tests for calendar_integration.containers."""

import pytest
from dependency_injector import providers

from calendar_integration.containers import CalendarContainer
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.calendar_side_effects_service import CalendarSideEffectsService
from di_core.containers import AppContainer
from webhooks.services import WebhookCalendarEventSideEffectsService


CALENDAR_PROVIDER_NAMES = [
    "calendar_side_effects_service",
    "calendar_permission_service",
    "external_event_change_request_service",
    "booking_policy_service",
    "booking_policy_permission_service",
    "external_client_identifier_service",
    "calendar_service",
    "bookable_slots_service",
    "appointment_type_service",
]


@pytest.mark.parametrize("name", CALENDAR_PROVIDER_NAMES)
def test_app_container_alias_is_calendar_container_provider(name: str) -> None:
    assert getattr(AppContainer, name) is getattr(CalendarContainer, name)


def test_calendar_side_effects_pipeline_holds_resolved_handler() -> None:
    """The pipeline holds a handler instance, not a Provider object."""
    service = AppContainer().calendar_side_effects_service()

    assert isinstance(service, CalendarSideEffectsService)
    assert len(service.side_effects_pipeline) == 1
    assert isinstance(service.side_effects_pipeline[0], WebhookCalendarEventSideEffectsService)
    assert not isinstance(service.side_effects_pipeline[0], providers.Provider)


def test_calendar_container_resolves_on_its_own() -> None:
    assert isinstance(CalendarContainer().calendar_service(), CalendarService)


def test_calendar_service_receives_upstream_providers() -> None:
    container = AppContainer()
    audit, entitlement = object(), object()

    with (
        container.audit_service.override(providers.Object(audit)),
        container.entitlement_service.override(providers.Object(entitlement)),
    ):
        service = container.calendar_service()

    assert service.audit_service is audit
    assert service.entitlement_service is entitlement
