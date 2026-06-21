"""Audit-emission tests for ``CalendarBundleService`` business writes.

Each test drives the real bundle sub-service (its auth context carries the
DI-injected ``audit_service``) and asserts that the expected audit record is
enqueued for the business write. We patch ``audit.services.persist_audit_record``
and execute the on_commit callbacks (the record() write path only fires on
transaction commit), then inspect the serialized payloads.

Only the five instrumented BUSINESS writes are asserted here:
``create_bundle_calendar`` / ``update_bundle_calendar`` (Calendar subject) and
``create_bundle_event`` / ``update_bundle_event`` / ``delete_bundle_event``
(CalendarEvent subject). The mechanical fan-out (child relationships, BlockedTime
representations) is intentionally NOT audited, so each public call must emit
exactly one business record.
"""

import datetime
from unittest.mock import patch

import pytest

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    ChildrenCalendarRelationship,
)
from calendar_integration.services.calendar_bundle_service import CalendarBundleService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    AvailableTimeWindow,
    CalendarEventInputData,
)
from organizations.models import Organization


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Bundle Audit Org", should_sync_rooms=False)


@pytest.fixture
def child_calendar_internal(organization, db):
    return Calendar.objects.create(
        name="Internal Child Calendar",
        external_id="audit-internal-child-1",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        organization=organization,
    )


@pytest.fixture
def child_calendar_google(organization, db):
    return Calendar.objects.create(
        name="Google Child Calendar",
        external_id="audit-google-child-1",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        organization=organization,
    )


@pytest.fixture
def initialized_facade(organization):
    service = CalendarService()
    service.initialize_without_provider(organization=organization)
    return service


@pytest.fixture
def bundle_service(initialized_facade):
    return CalendarBundleService(
        context=initialized_facade._build_context_snapshot(),
        host=initialized_facade,
    )


@pytest.fixture
def bundle_calendar(initialized_facade, child_calendar_internal, child_calendar_google):
    return initialized_facade.create_bundle_calendar(
        name="Audit Bundle Calendar",
        description="A bundle calendar",
        child_calendars=[child_calendar_internal, child_calendar_google],
        primary_calendar=child_calendar_google,
    )


@pytest.fixture
def bundle_event_data():
    return CalendarEventInputData(
        title="Bundle Meeting",
        description="A meeting created through bundle calendar",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payloads(mock_task) -> list[dict]:
    return [call.args[0] for call in mock_task.delay.call_args_list]


def _make_primary_event(
    organization: Organization,
    calendar: Calendar,
    bundle_calendar: Calendar,
    external_id: str = "audit-primary-event-1",
) -> CalendarEvent:
    return CalendarEvent.objects.create(
        title="Original Title",
        description="Original description",
        start_time_tz_unaware=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time_tz_unaware=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        calendar=calendar,
        organization=organization,
        external_id=external_id,
        is_bundle_primary=True,
        bundle_calendar=bundle_calendar,
    )


# ===========================================================================
# create_bundle_calendar
# ===========================================================================


@pytest.mark.django_db
def test_create_bundle_calendar_records_create(
    bundle_service,
    organization,
    child_calendar_internal,
    child_calendar_google,
    django_capture_on_commit_callbacks,
):
    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            cal = bundle_service.create_bundle_calendar(
                name="Audited Bundle",
                child_calendars=[child_calendar_internal, child_calendar_google],
                primary_calendar=child_calendar_google,
            )

    payloads = _payloads(mock_task)
    # Exactly one BUSINESS record: the bundle Calendar create. Child relationship
    # rows are mechanical and must NOT be audited.
    assert len(payloads) == 1
    record = payloads[0]
    assert record["organization_id"] == organization.id
    assert record["action"] == "create"
    assert record["subject"]["subject_type"] == "calendar_integration.Calendar"
    assert record["subject"]["subject_id"] == str(cal.id)
    assert record["subject"]["subject_label"] == "Audited Bundle"
    assert record["diff"] is None


# ===========================================================================
# update_bundle_calendar
# ===========================================================================


@pytest.mark.django_db
def test_update_bundle_calendar_records_update(
    bundle_service,
    organization,
    bundle_calendar,
    child_calendar_internal,
    child_calendar_google,
    django_capture_on_commit_callbacks,
):
    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            bundle_service.update_bundle_calendar(
                bundle_calendar=bundle_calendar,
                child_calendars=[child_calendar_google],
                primary_calendar=child_calendar_google,
            )

    payloads = _payloads(mock_task)
    assert len(payloads) == 1
    record = payloads[0]
    assert record["organization_id"] == organization.id
    assert record["action"] == "update"
    assert record["subject"]["subject_type"] == "calendar_integration.Calendar"
    assert record["subject"]["subject_id"] == str(bundle_calendar.id)
    # This method only reconciles child relationships -> no field-level diff.
    assert record["diff"] is None
    # Relationship reconciliation itself is not audited.
    assert ChildrenCalendarRelationship.objects.filter(bundle_calendar=bundle_calendar).count() == 1


# ===========================================================================
# create_bundle_event
# ===========================================================================


@pytest.mark.django_db
def test_create_bundle_event_records_single_create(
    initialized_facade,
    organization,
    bundle_calendar,
    child_calendar_google,
    bundle_event_data,
    django_capture_on_commit_callbacks,
):
    service = CalendarBundleService(
        context=initialized_facade._build_context_snapshot(),
        host=initialized_facade,
    )

    availability_window = [
        AvailableTimeWindow(
            start_time=bundle_event_data.start_time,
            end_time=bundle_event_data.end_time,
        )
    ]

    counter = {"n": 0}

    def fake_create_event(calendar_id: int, event_data: CalendarEventInputData) -> CalendarEvent:
        counter["n"] += 1
        cal = Calendar.objects.get(id=calendar_id, organization=organization)
        evt = CalendarEvent(
            title=event_data.title,
            calendar=cal,
            organization=organization,
            start_time_tz_unaware=event_data.start_time,
            end_time_tz_unaware=event_data.end_time,
            timezone="UTC",
            external_id=f"audit-fake-{counter['n']}",
        )
        evt.save()
        return evt

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            with patch.object(
                initialized_facade,
                "get_availability_windows_in_range",
                return_value=availability_window,
            ):
                with patch.object(
                    initialized_facade, "create_event", side_effect=fake_create_event
                ):
                    primary_event = service.create_bundle_event(bundle_calendar, bundle_event_data)

    payloads = _payloads(mock_task)
    # Exactly one BUSINESS record: the primary CalendarEvent. The fanned-out
    # representation events / BlockedTimes are mechanical and not audited here
    # (create_event is stubbed, so the child event service does not emit either).
    assert len(payloads) == 1
    record = payloads[0]
    assert record["organization_id"] == organization.id
    assert record["action"] == "create"
    assert record["subject"]["subject_type"] == "calendar_integration.CalendarEvent"
    assert record["subject"]["subject_id"] == str(primary_event.id)
    assert record["subject"]["subject_label"] == primary_event.title


# ===========================================================================
# update_bundle_event
# ===========================================================================


@pytest.mark.django_db
def test_update_bundle_event_records_update(
    initialized_facade,
    organization,
    bundle_calendar,
    django_capture_on_commit_callbacks,
):
    service = CalendarBundleService(
        context=initialized_facade._build_context_snapshot(),
        host=initialized_facade,
    )

    primary_cal = Calendar.objects.create(
        name="Primary Calendar",
        external_id="audit-primary-up-1",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )
    primary_event = _make_primary_event(
        organization, primary_cal, bundle_calendar, "audit-pev-up-1"
    )

    event_data = CalendarEventInputData(
        title="Updated Title",
        description="Updated description",
        start_time=datetime.datetime(2025, 6, 22, 14, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 15, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )

    def fake_update_event(
        calendar_id: int, event_id: int, data: CalendarEventInputData
    ) -> CalendarEvent:
        evt = CalendarEvent.objects.get(id=event_id, organization=organization)
        evt.title = data.title
        evt.description = data.description
        evt.save(update_fields=["title", "description"])
        return evt

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            with patch.object(initialized_facade, "update_event", side_effect=fake_update_event):
                service.update_bundle_event(primary_event, event_data)

    payloads = _payloads(mock_task)
    assert len(payloads) == 1
    record = payloads[0]
    assert record["organization_id"] == organization.id
    assert record["action"] == "update"
    assert record["subject"]["subject_type"] == "calendar_integration.CalendarEvent"
    assert record["subject"]["subject_id"] == str(primary_event.id)
    # title + description changed; timezone unchanged. start/end intentionally omitted.
    assert record["diff"] is not None
    assert record["diff"]["title"] == {"old": "Original Title", "new": "Updated Title"}
    assert record["diff"]["description"] == {
        "old": "Original description",
        "new": "Updated description",
    }
    assert "timezone" not in record["diff"]
    assert "start_time" not in record["diff"]
    assert "end_time" not in record["diff"]


# ===========================================================================
# delete_bundle_event
# ===========================================================================


@pytest.mark.django_db
def test_delete_bundle_event_records_delete(
    initialized_facade,
    organization,
    bundle_calendar,
    django_capture_on_commit_callbacks,
):
    service = CalendarBundleService(
        context=initialized_facade._build_context_snapshot(),
        host=initialized_facade,
    )

    primary_cal = Calendar.objects.create(
        name="Primary Calendar",
        external_id="audit-primary-del-1",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )
    primary_event = _make_primary_event(
        organization, primary_cal, bundle_calendar, "audit-pev-del-1"
    )
    primary_event_id = primary_event.id

    def fake_delete_event(calendar_id: int, event_id: int, delete_series: bool = False) -> None:
        CalendarEvent.objects.filter(id=event_id, organization=organization).delete()

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            with patch.object(initialized_facade, "delete_event", side_effect=fake_delete_event):
                service.delete_bundle_event(primary_event)

    payloads = _payloads(mock_task)
    assert len(payloads) == 1
    record = payloads[0]
    assert record["organization_id"] == organization.id
    assert record["action"] == "delete"
    assert record["subject"]["subject_type"] == "calendar_integration.CalendarEvent"
    # Subject pk must reference the now-deleted primary event row.
    assert record["subject"]["subject_id"] == str(primary_event_id)
    assert record["subject"]["subject_label"] == "Original Title"
