"""Unit + integration tests for ExternalClientIdentifier.

Covers:
- unit: both unique constraints raise IntegrityError on violation;
- unit: two records of different content types may share one (system, identifier);
- unit: two organizations may each hold the same (system, identifier);
- unit: normalize_system handles case, trailing slash, and preserves path/query/fragment;
- integration: deleting a CalendarEvent / ExternalAttendee deletes its identifier rows;
- integration: deleting a Calendar cascades through its events to their identifier rows
  (the parent-driven cascade a post_delete signal would have missed).
"""

from __future__ import annotations

import datetime

from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction

import pytest
from model_bakery import baker

from calendar_integration.external_client_identifiers import normalize_system
from calendar_integration.factories import create_external_client_identifier
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    ExternalAttendee,
    ExternalClientIdentifier,
)
from organizations.models import Organization


@pytest.fixture
def organization(db) -> Organization:
    return baker.make(Organization)


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    return baker.make(Calendar, organization=organization)


@pytest.fixture
def event(organization: Organization, calendar: Calendar) -> CalendarEvent:
    return baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        title="Test Event",
        external_id="event-test-1",
        start_time_tz_unaware=datetime.datetime(2026, 1, 1, 9, 0, 0),
        end_time_tz_unaware=datetime.datetime(2026, 1, 1, 10, 0, 0),
        timezone="UTC",
    )


@pytest.fixture
def external_attendee(organization: Organization) -> ExternalAttendee:
    return baker.make(ExternalAttendee, organization=organization, email="attendee@example.com")


# -- Unit tests: constraints ------------------------------------------------


@pytest.mark.django_db
def test_uniq_target_system_constraint_rejects_duplicate(
    organization: Organization, event: CalendarEvent
) -> None:
    """A second identifier for the same (target, system) pair violates the constraint."""
    create_external_client_identifier(
        organization=organization,
        identified_object=event,
        system="https://crm.example.com",
        identifier="deal-1",
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            create_external_client_identifier(
                organization=organization,
                identified_object=event,
                system="https://crm.example.com",
                identifier="deal-2",
            )


@pytest.mark.django_db
def test_uniq_system_ident_constraint_rejects_duplicate(
    organization: Organization, calendar: Calendar, event: CalendarEvent
) -> None:
    """A second record of the same type claiming the same (system, identifier) violates it."""
    other_event = baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        title="Other Event",
        external_id="event-test-2",
        start_time_tz_unaware=datetime.datetime(2026, 2, 1, 9, 0, 0),
        end_time_tz_unaware=datetime.datetime(2026, 2, 1, 10, 0, 0),
        timezone="UTC",
    )
    create_external_client_identifier(
        organization=organization,
        identified_object=event,
        system="https://crm.example.com",
        identifier="deal-1",
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            create_external_client_identifier(
                organization=organization,
                identified_object=other_event,
                system="https://crm.example.com",
                identifier="deal-1",
            )


@pytest.mark.django_db
def test_different_content_types_may_share_system_and_identifier(
    organization: Organization, event: CalendarEvent, external_attendee: ExternalAttendee
) -> None:
    """The (organization, content_type, system, identifier) constraint is per content type."""
    event_identifier = create_external_client_identifier(
        organization=organization,
        identified_object=event,
        system="https://crm.example.com",
        identifier="shared-id",
    )
    attendee_identifier = create_external_client_identifier(
        organization=organization,
        identified_object=external_attendee,
        system="https://crm.example.com",
        identifier="shared-id",
    )

    assert event_identifier.pk != attendee_identifier.pk
    assert event_identifier.content_type != attendee_identifier.content_type


@pytest.mark.django_db
def test_two_organizations_may_share_system_and_identifier(
    organization: Organization, event: CalendarEvent
) -> None:
    """The organization column participates in both constraints, so a cross-tenant collision
    on (system, identifier) is not a collision at all."""
    other_organization = baker.make(Organization)
    other_calendar = baker.make(Calendar, organization=other_organization)
    other_event = baker.make(
        CalendarEvent,
        organization=other_organization,
        calendar=other_calendar,
        title="Other Org Event",
        external_id="event-other-org-1",
        start_time_tz_unaware=datetime.datetime(2026, 1, 1, 9, 0, 0),
        end_time_tz_unaware=datetime.datetime(2026, 1, 1, 10, 0, 0),
        timezone="UTC",
    )

    first = create_external_client_identifier(
        organization=organization,
        identified_object=event,
        system="https://crm.example.com",
        identifier="shared-id",
    )
    second = create_external_client_identifier(
        organization=other_organization,
        identified_object=other_event,
        system="https://crm.example.com",
        identifier="shared-id",
    )

    assert first.pk != second.pk
    assert first.organization_id != second.organization_id
    assert first.system == second.system
    assert first.identifier == second.identifier


# -- Unit tests: normalize_system --------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://CRM.Example.COM", "https://crm.example.com"),
        ("HTTPS://crm.example.com", "https://crm.example.com"),
        ("https://crm.example.com/", "https://crm.example.com"),
        ("https://crm.example.com/api/", "https://crm.example.com/api"),
        ("https://crm.example.com/api", "https://crm.example.com/api"),
        (
            "https://crm.example.com/api?foo=bar#frag",
            "https://crm.example.com/api?foo=bar#frag",
        ),
        (
            "https://Crm.Example.com/api/v1/?foo=Bar#Frag",
            "https://crm.example.com/api/v1?foo=Bar#Frag",
        ),
    ],
)
def test_normalize_system(value: str, expected: str) -> None:
    """Scheme and host are lowercased; a trailing slash is stripped from the path;
    path/query/fragment are otherwise preserved verbatim (including case)."""
    assert normalize_system(value) == expected


# -- Integration tests: cascade behavior -------------------------------------


@pytest.mark.django_db
def test_deleting_event_deletes_its_identifier_rows(
    organization: Organization, event: CalendarEvent
) -> None:
    """Deleting a CalendarEvent removes its ExternalClientIdentifier rows."""
    identifier = create_external_client_identifier(
        organization=organization,
        identified_object=event,
        system="https://crm.example.com",
        identifier="deal-1",
    )

    event.delete()

    assert (
        not ExternalClientIdentifier.objects.filter_by_organization(organization)
        .filter(pk=identifier.pk)
        .exists()
    )


@pytest.mark.django_db
def test_deleting_external_attendee_deletes_its_identifier_rows(
    organization: Organization, external_attendee: ExternalAttendee
) -> None:
    """Deleting an ExternalAttendee removes its ExternalClientIdentifier rows."""
    identifier = create_external_client_identifier(
        organization=organization,
        identified_object=external_attendee,
        system="https://crm.example.com",
        identifier="contact-1",
    )

    external_attendee.delete()

    assert (
        not ExternalClientIdentifier.objects.filter_by_organization(organization)
        .filter(pk=identifier.pk)
        .exists()
    )


@pytest.mark.django_db
def test_deleting_calendar_cascades_through_events_to_identifier_rows(
    organization: Organization, calendar: Calendar, event: CalendarEvent
) -> None:
    """Deleting a Calendar cascades to its events and, from there, to their identifier
    rows -- the parent-driven cascade a post_delete signal on CalendarEvent would miss."""
    other_event = baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        title="Second Event",
        external_id="event-test-3",
        start_time_tz_unaware=datetime.datetime(2026, 3, 1, 9, 0, 0),
        end_time_tz_unaware=datetime.datetime(2026, 3, 1, 10, 0, 0),
        timezone="UTC",
    )
    identifier_1 = create_external_client_identifier(
        organization=organization,
        identified_object=event,
        system="https://crm.example.com",
        identifier="deal-1",
    )
    identifier_2 = create_external_client_identifier(
        organization=organization,
        identified_object=other_event,
        system="https://crm.example.com",
        identifier="deal-2",
    )

    calendar.delete()

    remaining_pks = set(
        ExternalClientIdentifier.objects.filter_by_organization(organization).values_list(
            "pk", flat=True
        )
    )
    assert identifier_1.pk not in remaining_pks
    assert identifier_2.pk not in remaining_pks


@pytest.mark.django_db
def test_content_type_resolved_lazily_for_calendar_event(event: CalendarEvent) -> None:
    """The ContentType for CalendarEvent is whatever the test database assigned it --
    never a hard-coded id."""
    identifier = create_external_client_identifier(
        organization=event.organization,
        identified_object=event,
        system="https://crm.example.com",
        identifier="deal-1",
    )

    assert identifier.content_type == ContentType.objects.get_for_model(CalendarEvent)
    assert identifier.identified_key == event.pk
