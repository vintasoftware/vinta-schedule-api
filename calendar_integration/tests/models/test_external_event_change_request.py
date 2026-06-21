"""Unit tests for ExternalEventChangeRequest model (Phase 2).

Covers:
- factory builds a valid instance;
- partial unique constraint rejects a second PENDING row for the same event;
- partial unique constraint allows non-PENDING rows alongside PENDING
  (STALE + PENDING coexist; two STALE coexist);
- factory raises on organization mismatch;
- __str__ does not crash even when event is null (historical/resolved row);
- queryset pending() and for_event() filtering.
"""

from __future__ import annotations

import datetime

from django.db import IntegrityError, transaction

import pytest
from model_bakery import baker

from calendar_integration.constants import (
    CalendarProvider,
    ExternalEventChangeKind,
    ExternalEventChangeRequestStatus,
)
from calendar_integration.factories import create_external_event_change_request
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    ExternalEventChangeRequest,
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


@pytest.mark.django_db
def test_factory_creates_valid_pending_request(event: CalendarEvent) -> None:
    """Factory produces a valid PENDING ExternalEventChangeRequest."""
    request = create_external_event_change_request(event=event)

    assert request.pk is not None
    assert request.organization == event.organization
    assert request.event_fk_id == event.pk
    assert request.kind == ExternalEventChangeKind.UPDATE
    assert request.status == ExternalEventChangeRequestStatus.PENDING
    assert request.provider == CalendarProvider.GOOGLE
    assert request.proposed_values == {}
    assert request.proposed_payload == {}
    assert request.retained_values == {}
    assert request.resolved_by_user_id is None
    assert request.resolved_at is None


@pytest.mark.django_db
def test_factory_creates_delete_kind_request(event: CalendarEvent) -> None:
    """Factory accepts a delete-kind request."""
    request = create_external_event_change_request(
        event=event,
        kind=ExternalEventChangeKind.DELETE,
        retained_values={"title": "Old Title"},
    )

    assert request.kind == ExternalEventChangeKind.DELETE
    assert request.retained_values == {"title": "Old Title"}


@pytest.mark.django_db
def test_factory_raises_on_organization_mismatch(
    event: CalendarEvent,
) -> None:
    """Factory raises ValueError when a mismatched organization is passed."""
    other_org = baker.make(Organization)

    with pytest.raises(ValueError, match="organization mismatch"):
        create_external_event_change_request(event=event, organization=other_org)


@pytest.mark.django_db
def test_str_does_not_crash(event: CalendarEvent) -> None:
    """__str__ contains 'ExternalEventChangeRequest' regardless of event nullability."""
    request = create_external_event_change_request(event=event)
    assert "ExternalEventChangeRequest" in str(request)


@pytest.mark.django_db
def test_str_does_not_crash_when_event_is_null(event: CalendarEvent) -> None:
    """__str__ does not crash when event_fk_id is None (historical row after event deletion)."""
    request = create_external_event_change_request(event=event)
    # Simulate a historical row where the CalendarEvent has been deleted and set to NULL.
    ExternalEventChangeRequest.objects.filter(pk=request.pk).update(event_fk=None)
    request.refresh_from_db()
    assert "deleted" in str(request)
    assert "ExternalEventChangeRequest" in str(request)


@pytest.mark.django_db
def test_partial_unique_constraint_rejects_second_pending_for_same_event(
    event: CalendarEvent,
) -> None:
    """A second PENDING row for the same event violates the partial unique constraint."""
    create_external_event_change_request(
        event=event, status=ExternalEventChangeRequestStatus.PENDING
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            create_external_event_change_request(
                event=event, status=ExternalEventChangeRequestStatus.PENDING
            )


@pytest.mark.django_db
def test_partial_unique_constraint_allows_stale_alongside_pending(
    event: CalendarEvent,
) -> None:
    """A STALE and a PENDING request can coexist for the same event."""
    stale = create_external_event_change_request(
        event=event, status=ExternalEventChangeRequestStatus.STALE
    )
    pending = create_external_event_change_request(
        event=event, status=ExternalEventChangeRequestStatus.PENDING
    )

    assert (
        ExternalEventChangeRequest.objects.filter(
            organization=event.organization, event_fk_id=event.pk
        ).count()
        == 2
    )
    assert stale.status == ExternalEventChangeRequestStatus.STALE
    assert pending.status == ExternalEventChangeRequestStatus.PENDING


@pytest.mark.django_db
def test_partial_unique_constraint_allows_two_stale_for_same_event(
    event: CalendarEvent,
) -> None:
    """Two STALE requests can coexist for the same event (constraint is only on PENDING)."""
    stale_1 = create_external_event_change_request(
        event=event, status=ExternalEventChangeRequestStatus.STALE
    )
    stale_2 = create_external_event_change_request(
        event=event, status=ExternalEventChangeRequestStatus.STALE
    )

    assert stale_1.pk != stale_2.pk
    assert (
        ExternalEventChangeRequest.objects.filter(
            organization=event.organization,
            event_fk_id=event.pk,
            status=ExternalEventChangeRequestStatus.STALE,
        ).count()
        == 2
    )


@pytest.mark.django_db
def test_queryset_pending_filters_to_pending_only(event: CalendarEvent) -> None:
    """ExternalEventChangeRequest.objects.pending() returns only PENDING rows."""
    pending = create_external_event_change_request(
        event=event, status=ExternalEventChangeRequestStatus.PENDING
    )
    stale = create_external_event_change_request(
        event=event, status=ExternalEventChangeRequestStatus.STALE
    )

    pending_qs = ExternalEventChangeRequest.objects.filter(
        organization=event.organization
    ).pending()

    assert pending.pk in pending_qs.values_list("pk", flat=True)
    assert stale.pk not in pending_qs.values_list("pk", flat=True)


@pytest.mark.django_db
def test_queryset_for_event_filters_to_event(
    organization: Organization, calendar: Calendar, event: CalendarEvent
) -> None:
    """ExternalEventChangeRequest.objects.for_event(event) returns only rows for that event."""
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

    req_for_event = create_external_event_change_request(event=event)
    req_for_other = create_external_event_change_request(
        event=other_event, status=ExternalEventChangeRequestStatus.STALE
    )

    qs = ExternalEventChangeRequest.objects.filter(organization=organization).for_event(event)

    pks = list(qs.values_list("pk", flat=True))
    assert req_for_event.pk in pks
    assert req_for_other.pk not in pks
