"""Direct unit tests for the stateless ``RecurrenceManager`` engines.

These exercise the two generic template-method engines in isolation with a
synthetic set of callbacks, asserting the engine drives the callbacks and
returns the expected structure. The end-to-end behavior of the entity-specific
recurrence methods (events / blocked times / available times) stays covered by
``test_calendar_service.py`` against the facade.
"""

import datetime
from typing import Any

import pytest

from calendar_integration.constants import CalendarProvider, RecurrenceFrequency
from calendar_integration.exceptions import CalendarServiceOrganizationNotSetError
from calendar_integration.factories import CalendarEventFactory
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    EventRecurrenceException,
    RecurrenceRule,
    RecurringMixin,
)
from calendar_integration.services.calendar_service_context import CalendarServiceContext
from calendar_integration.services.recurrence_manager import RecurrenceManager
from organizations.models import Organization


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Recurrence Org", should_sync_rooms=False)


@pytest.fixture
def calendar(db, organization):
    return Calendar.objects.create(
        name="Recurrence Calendar",
        description="",
        external_id="rec_cal_1",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )


@pytest.fixture
def authenticated_context(organization) -> CalendarServiceContext:
    """A context whose organization passes the auth guard."""
    return CalendarServiceContext(
        organization=organization,
        user_or_token=None,
        account=None,
        calendar_adapter=None,
        calendar_permission_service=None,
        calendar_side_effects_service=None,
    )


@pytest.fixture
def weekly_event(calendar) -> CalendarEvent:
    """A weekly recurring event with 5 occurrences starting on a Monday."""
    start = datetime.datetime(2025, 6, 23, 10, 0, tzinfo=datetime.UTC)  # Monday
    end = datetime.datetime(2025, 6, 23, 11, 0, tzinfo=datetime.UTC)
    return CalendarEventFactory.create_recurring_event(
        calendar=calendar,
        title="Weekly Sync",
        description="recurring",
        start_time=start,
        end_time=end,
        frequency=RecurrenceFrequency.WEEKLY,
        count=5,
        by_weekday="MO",
        external_id="weekly_master_1",
    )


@pytest.mark.django_db
def test_exception_engine_cancelled_future_occurrence(authenticated_context, weekly_event):
    """A cancelled exception on a future occurrence records the exception and returns None."""
    manager = RecurrenceManager()
    calls: dict[str, int] = {"create_modified": 0}

    def create_modified_object_callback(
        parent_obj: RecurringMixin,
        exception_datetime: datetime.datetime,
        modification_data: dict[str, Any],
    ) -> RecurringMixin:
        calls["create_modified"] += 1
        raise AssertionError("create_modified_object_callback should not run when cancelling")

    # Second occurrence: one week after the master start.
    exception_date = weekly_event.start_time.date() + datetime.timedelta(weeks=1)

    result = manager.create_recurring_exception_generic(
        authenticated_context,
        object_type_name="event",
        parent_object=weekly_event,
        exception_date=exception_date,
        is_cancelled=True,
        create_modified_object_callback=create_modified_object_callback,
    )

    assert result is None
    assert calls["create_modified"] == 0
    exception = weekly_event.recurrence_exceptions.first()
    assert exception is not None
    assert exception.is_cancelled is True


@pytest.mark.django_db
def test_exception_engine_modified_future_occurrence_invokes_callback(
    authenticated_context, calendar, weekly_event
):
    """A modified exception on a future occurrence runs the create-modified callback and records it."""
    manager = RecurrenceManager()
    recorded: dict[str, Any] = {}

    def create_modified_object_callback(
        parent_obj: RecurringMixin,
        exception_datetime: datetime.datetime,
        modification_data: dict[str, Any],
    ) -> RecurringMixin:
        recorded["parent"] = parent_obj
        recorded["exception_datetime"] = exception_datetime
        recorded["modification_data"] = modification_data
        modified = CalendarEvent.objects.create(
            calendar_fk=calendar,
            organization=calendar.organization,
            external_id="modified_instance_1",
            title=modification_data.get("title") or parent_obj.title,
            description=parent_obj.description,
            start_time_tz_unaware=exception_datetime,
            end_time_tz_unaware=exception_datetime + datetime.timedelta(hours=1),
            timezone="UTC",
        )
        return modified

    exception_date = weekly_event.start_time.date() + datetime.timedelta(weeks=1)

    result = manager.create_recurring_exception_generic(
        authenticated_context,
        object_type_name="event",
        parent_object=weekly_event,
        exception_date=exception_date,
        is_cancelled=False,
        modification_data={"title": "Modified Title"},
        create_modified_object_callback=create_modified_object_callback,
    )

    assert result is not None
    assert recorded["parent"] == weekly_event
    assert recorded["modification_data"] == {"title": "Modified Title"}
    assert result.is_recurring_exception is True
    assert result.title == "Modified Title"

    exception = weekly_event.recurrence_exceptions.first()
    assert exception is not None
    assert exception.is_cancelled is False
    assert exception.modified_event == result


@pytest.mark.django_db
def test_exception_engine_master_date_creates_continuation_and_demotes_master(
    authenticated_context, calendar, weekly_event
):
    """An exception on the master date spins off a continuation and demotes the master.

    Exercises the master-date branch: with a future occurrence present, the engine
    clones the recurrence rule (count decremented), fires the create-new-recurring
    callback for the second occurrence, fires the exception-manager update callback,
    deletes the original ``RecurrenceRule``, and re-fetches the master as non-recurring.
    """
    manager = RecurrenceManager()
    recorded: dict[str, Any] = {}
    original_rule_id = weekly_event.recurrence_rule.id
    original_count = weekly_event.recurrence_rule.count

    def create_new_recurring_callback(
        parent_obj: RecurringMixin,
        second_occurrence: RecurringMixin,
        new_recurrence_rule: RecurrenceRule,
    ) -> RecurringMixin:
        recorded["parent"] = parent_obj
        recorded["second_occurrence_start"] = second_occurrence.start_time
        recorded["new_rule_id"] = new_recurrence_rule.id
        recorded["new_rule_count"] = new_recurrence_rule.count
        new_recurrence_rule.save()
        continuation = CalendarEvent.objects.create(
            calendar_fk=calendar,
            organization=calendar.organization,
            external_id="master_continuation_1",
            title=parent_obj.title,
            description=parent_obj.description,
            start_time_tz_unaware=second_occurrence.start_time,
            end_time_tz_unaware=second_occurrence.start_time + datetime.timedelta(hours=1),
            timezone="UTC",
            recurrence_rule_fk=new_recurrence_rule,
        )
        return continuation

    def exception_manager_update_callback(
        parent_obj: RecurringMixin,
        new_recurring_object: RecurringMixin,
    ) -> None:
        recorded["update_parent"] = parent_obj
        recorded["update_new_object"] = new_recurring_object

    def exception_manager_delete_callback(parent_obj: RecurringMixin) -> None:
        recorded["delete_called"] = True

    exception_date = weekly_event.start_time.date()

    result = manager.create_recurring_exception_generic(
        authenticated_context,
        object_type_name="event",
        parent_object=weekly_event,
        exception_date=exception_date,
        is_cancelled=False,
        create_new_recurring_callback=create_new_recurring_callback,
        exception_manager_update_callback=exception_manager_update_callback,
        exception_manager_delete_callback=exception_manager_delete_callback,
    )

    # The create-new-recurring callback fired for the second occurrence.
    assert recorded["parent"] == weekly_event
    assert recorded["second_occurrence_start"] == weekly_event.start_time + datetime.timedelta(
        weeks=1
    )
    # The cloned rule is a fresh row with the count decremented by one.
    assert recorded["new_rule_id"] != original_rule_id
    assert recorded["new_rule_count"] == original_count - 1

    # The exception-manager update callback fired (delete branch did not run).
    assert recorded["update_parent"] == weekly_event
    assert recorded["update_new_object"] is not None
    assert "delete_called" not in recorded

    # The original RecurrenceRule was deleted.
    assert not RecurrenceRule.objects.filter(id=original_rule_id).exists()

    # The master was re-fetched and demoted to non-recurring.
    assert result is not None
    assert result.pk == weekly_event.pk
    assert result.is_recurring is False


@pytest.mark.django_db
def test_exception_engine_non_recurring_raises(authenticated_context, calendar):
    """The engine rejects a non-recurring parent object."""
    manager = RecurrenceManager()
    non_recurring = CalendarEvent.objects.create(
        calendar_fk=calendar,
        organization=calendar.organization,
        title="One off",
        description="",
        start_time_tz_unaware=datetime.datetime(2025, 6, 23, 10, 0, tzinfo=datetime.UTC),
        end_time_tz_unaware=datetime.datetime(2025, 6, 23, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
    )

    with pytest.raises(ValueError, match="non-recurring event"):
        manager.create_recurring_exception_generic(
            authenticated_context,
            object_type_name="event",
            parent_object=non_recurring,
            exception_date=non_recurring.start_time.date(),
            is_cancelled=True,
        )


@pytest.mark.django_db
def test_exception_engine_unauthenticated_context_raises(weekly_event):
    """A context without an organization fails the auth guard."""
    manager = RecurrenceManager()
    unauthenticated = CalendarServiceContext(
        organization=None,
        user_or_token=None,
        account=None,
        calendar_adapter=None,
        calendar_permission_service=None,
        calendar_side_effects_service=None,
    )

    with pytest.raises(CalendarServiceOrganizationNotSetError):
        manager.create_recurring_exception_generic(
            unauthenticated,
            object_type_name="event",
            parent_object=weekly_event,
            exception_date=weekly_event.start_time.date(),
            is_cancelled=True,
        )


@pytest.mark.django_db
def test_bulk_modification_engine_invokes_callbacks_and_returns_continuation(
    authenticated_context, calendar, weekly_event
):
    """The bulk engine truncates the parent, builds a continuation, and records the modification."""
    manager = RecurrenceManager()
    invoked: dict[str, Any] = {}

    def truncate_parent_callback(
        parent_obj: RecurringMixin,
        new_recurrence_rule: RecurrenceRule | None,
    ) -> RecurringMixin:
        invoked["truncate_rule"] = new_recurrence_rule
        parent = parent_obj
        parent.recurrence_rule_fk = new_recurrence_rule  # type: ignore[assignment]
        parent.save()
        return parent

    def create_continuation_callback(
        parent_obj: RecurringMixin,
        start_dt: datetime.datetime,
        recurrence_rule: RecurrenceRule | None,
        modification_data: dict[str, Any],
    ) -> RecurringMixin:
        invoked["continuation_start"] = start_dt
        invoked["continuation_rule"] = recurrence_rule
        # The engine has already persisted ``recurrence_rule`` for us; assert it
        # was handed over but do not re-bind it on a second event (the model's
        # recurrence_rule FK is one-to-one).
        continuation = CalendarEvent.objects.create(
            calendar_fk=calendar,
            organization=calendar.organization,
            external_id="continuation_1",
            title=modification_data.get("title") or parent_obj.title,
            description=parent_obj.description,
            start_time_tz_unaware=start_dt,
            end_time_tz_unaware=start_dt + datetime.timedelta(hours=1),
            timezone="UTC",
        )
        return continuation

    def bulk_modification_record_callback(
        parent_obj: RecurringMixin,
        start_dt: datetime.datetime,
        continuation_obj: RecurringMixin | None,
        cancelled: bool,
    ) -> None:
        invoked["record"] = {
            "parent": parent_obj,
            "start": start_dt,
            "continuation": continuation_obj,
            "cancelled": cancelled,
        }

    # Modify from the third occurrence (two weeks after the master start).
    modification_start_date = weekly_event.start_time + datetime.timedelta(weeks=2)

    result = manager.create_recurring_bulk_modification_generic(
        authenticated_context,
        object_type_name="event",
        parent_object=weekly_event,
        modification_start_date=modification_start_date,
        is_bulk_cancelled=False,
        modification_data={"title": "From here on"},
        truncate_parent_callback=truncate_parent_callback,
        create_continuation_callback=create_continuation_callback,
        bulk_modification_record_callback=bulk_modification_record_callback,
    )

    assert result is not None
    assert result.title == "From here on"
    assert "truncate_rule" in invoked
    assert invoked["continuation_start"] == modification_start_date
    assert invoked["record"]["cancelled"] is False
    assert invoked["record"]["continuation"] == result


@pytest.mark.django_db
def test_bulk_modification_engine_cancelled_skips_continuation(authenticated_context, weekly_event):
    """A cancelled bulk modification truncates the parent and records, but builds no continuation."""
    manager = RecurrenceManager()
    invoked: dict[str, Any] = {"continuation": 0}

    def truncate_parent_callback(
        parent_obj: RecurringMixin,
        new_recurrence_rule: RecurrenceRule | None,
    ) -> RecurringMixin:
        parent_obj.recurrence_rule_fk = new_recurrence_rule  # type: ignore[assignment]
        parent_obj.save()
        return parent_obj

    def create_continuation_callback(
        parent_obj: RecurringMixin,
        start_dt: datetime.datetime,
        recurrence_rule: RecurrenceRule | None,
        modification_data: dict[str, Any],
    ) -> RecurringMixin:
        invoked["continuation"] += 1
        raise AssertionError("continuation callback should not run when cancelling")

    def bulk_modification_record_callback(
        parent_obj: RecurringMixin,
        start_dt: datetime.datetime,
        continuation_obj: RecurringMixin | None,
        cancelled: bool,
    ) -> None:
        invoked["record_cancelled"] = cancelled
        invoked["record_continuation"] = continuation_obj

    modification_start_date = weekly_event.start_time + datetime.timedelta(weeks=2)

    result = manager.create_recurring_bulk_modification_generic(
        authenticated_context,
        object_type_name="event",
        parent_object=weekly_event,
        modification_start_date=modification_start_date,
        is_bulk_cancelled=True,
        truncate_parent_callback=truncate_parent_callback,
        create_continuation_callback=create_continuation_callback,
        bulk_modification_record_callback=bulk_modification_record_callback,
    )

    assert result is None
    assert invoked["continuation"] == 0
    assert invoked["record_cancelled"] is True
    assert invoked["record_continuation"] is None


@pytest.mark.django_db
def test_bulk_modification_engine_invalid_date_raises(authenticated_context, weekly_event):
    """A modification date that is not an occurrence is rejected."""
    manager = RecurrenceManager()
    # Tuesday — not a Monday occurrence of the weekly series.
    bad_date = weekly_event.start_time + datetime.timedelta(days=1)

    with pytest.raises(ValueError, match="not a valid occurrence"):
        manager.create_recurring_bulk_modification_generic(
            authenticated_context,
            object_type_name="event",
            parent_object=weekly_event,
            modification_start_date=bad_date,
        )


def test_recurrence_exception_model_importable():
    """Smoke check that the exception model referenced by event callbacks is importable."""
    assert EventRecurrenceException is not None
