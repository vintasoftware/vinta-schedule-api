"""Unit tests for CalendarGroup duration pinning.

History correction: an earlier draft of this design put a nullable
``duration`` column on ``CalendarManagementToken`` and enforced it as a
per-code pin. That was wrong in one decisive way: a **codeless** public-group
booking (``CalendarGroup.accepts_public_scheduling=True``) presents no code,
so it inherits no pin -- the one booking path reachable with no credential
was also the one path with no length constraint. Duration is a property of
the thing being booked (the ``CalendarGroup``), not of the invitation to book
it (the code). This file replaces the old token-duration test module.

Covers the enforcement sites:

- ``CalendarPermissionService.can_perform_group_scheduling`` -- the
  times-aware overload, including the ordering regression test: the pin must
  be enforced BEFORE the ``accepts_public_scheduling`` short-circuit, which
  returns ``True`` without ever reading anything else.
- ``CalendarPermissionService.can_perform_update`` -- against ``new_event``'s
  span for a GROUPED event (``calendar_group_id`` resolved from
  ``CalendarEvent.calendar_group_fk_id``), unaffected by cancellation
  (``new_event is None``) and by non-grouped events (``calendar_group_id is
  None``).
- ``CalendarPermissionService.can_perform_scheduling`` (single calendar) --
  carries NO duration pin at all any more. There is no ``Calendar.duration``;
  pinning was dropped for this path rather than relocated, because there is
  no codeless single-calendar booking path to begin with (a single-calendar
  booking always requires a code, so there was never an unconstrained hole
  here).
- The fail-closed rule: a public group (``accepts_public_scheduling=True``)
  with ``duration=None`` refuses booking outright rather than allowing any
  length -- a misconfigured public group must surface immediately, not
  silently reopen the unbounded-length hole this whole mechanism exists to
  close.
- A private group with ``duration=None`` is unaffected by any of this --
  matching every group created before this field existed.

Also asserts ``CalendarManagementToken`` has no ``duration`` attribute at
all, closing the loop on the history correction.
"""

import datetime

from django.utils import timezone

import pytest
from model_bakery import baker

from calendar_integration.constants import EventManagementPermissions
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarManagementToken,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.dataclasses import (
    CalendarEventData,
    CalendarEventInputData,
    CalendarSettingsData,
)
from organizations.models import Organization


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def org(db) -> Organization:
    return baker.make(Organization)


@pytest.fixture
def calendar(org) -> Calendar:
    return Calendar.objects.create(name="Test Calendar", organization=org)


@pytest.fixture
def calendar_group(org) -> CalendarGroup:
    return CalendarGroup.objects.create(name="Test Group", organization=org)


@pytest.fixture
def event(org, calendar) -> CalendarEvent:
    now = timezone.now()
    return CalendarEvent.objects.create(
        organization=org,
        calendar_fk=calendar,
        title="Test Event",
        start_time_tz_unaware=now,
        end_time_tz_unaware=now + datetime.timedelta(minutes=30),
        timezone="UTC",
    )


@pytest.fixture
def service() -> CalendarPermissionService:
    return CalendarPermissionService()


def _restricted_settings() -> CalendarSettingsData:
    return CalendarSettingsData(manage_available_windows=True, accepts_public_scheduling=False)


def _public_settings() -> CalendarSettingsData:
    return CalendarSettingsData(manage_available_windows=False, accepts_public_scheduling=True)


def _event_input(start: datetime.datetime, span: datetime.timedelta) -> CalendarEventInputData:
    return CalendarEventInputData(
        title="Test",
        description="",
        start_time=start,
        end_time=start + span,
        timezone="UTC",
    )


def _event_data(event: CalendarEvent, start: datetime.datetime, span: datetime.timedelta):
    assert event.calendar_fk_id is not None
    return CalendarEventData(
        id=event.id,
        calendar_id=event.calendar_fk_id,
        title=event.title,
        description="",
        start_time=start,
        end_time=start + span,
        timezone="UTC",
        attendees=[],
        external_attendees=[],
        resources=[],
        recurrence_rule=None,
        external_id="test_external_id",
        calendar_settings=None,
        status="confirmed",
        is_recurring=False,
        recurring_event_id=None,
        original_payload=None,
    )


# ---------------------------------------------------------------------------
# CalendarManagementToken carries no duration at all
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_calendar_management_token_has_no_duration_attribute(org, calendar):
    """History-correction guard: duration lives on CalendarGroup now, never on
    CalendarManagementToken. If this attribute ever comes back, something
    reintroduced the per-code pin the codeless public-group hole was fixed by
    removing."""
    token = CalendarManagementToken.objects.create(
        organization=org,
        calendar_fk=calendar,
        token_hash="irrelevant",
    )
    assert not hasattr(token, "duration")


# ---------------------------------------------------------------------------
# can_perform_scheduling (single calendar) -- no duration pin at all
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_can_perform_scheduling_single_calendar_carries_no_duration_pin(service, org, calendar):
    """Single-calendar codes are never duration-constrained -- any span is
    authorized exactly as it always was; there is no Calendar.duration and
    pinning was dropped for this path rather than relocated."""
    token, _ = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )
    service.token = token
    start = timezone.now()

    result = service.can_perform_scheduling(
        calendar_id=calendar.id,
        calendar_settings=_restricted_settings(),
        event=_event_input(start, datetime.timedelta(hours=5)),
    )
    assert result is True


@pytest.mark.django_db
def test_can_perform_scheduling_no_token_public_calendar_unchanged(service, org, calendar):
    """No token at all (the ``accepts_public_scheduling`` codeless path) is unaffected."""
    result = service.can_perform_scheduling(
        calendar_id=calendar.id,
        calendar_settings=_public_settings(),
        event=_event_input(timezone.now(), datetime.timedelta(hours=3)),
    )
    assert result is True


# ---------------------------------------------------------------------------
# can_perform_update -- grouped event, checked against the GROUP's duration
# ---------------------------------------------------------------------------


@pytest.fixture
def grouped_event(org, calendar, calendar_group) -> CalendarEvent:
    """An event booked through ``calendar_group`` -- the fixture that lets
    ``can_perform_update`` resolve the group from ``calendar_group_fk_id``."""
    now = timezone.now()
    return CalendarEvent.objects.create(
        organization=org,
        calendar_fk=calendar,
        calendar_group_fk=calendar_group,
        title="Grouped Event",
        start_time_tz_unaware=now,
        end_time_tz_unaware=now + datetime.timedelta(minutes=30),
        timezone="UTC",
    )


@pytest.mark.django_db
def test_can_perform_update_rejects_new_span_mismatch_for_grouped_event(
    service, org, calendar, calendar_group, grouped_event
):
    calendar_group.duration = datetime.timedelta(minutes=30)
    calendar_group.save(update_fields=["duration"])

    token, _ = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_id=calendar.id,
        event_id=grouped_event.id,
    )
    service.token = token
    start = timezone.now()
    old_event = _event_data(grouped_event, start, datetime.timedelta(minutes=30))
    new_event = _event_data(
        grouped_event, start + datetime.timedelta(hours=1), datetime.timedelta(minutes=45)
    )

    assert (
        service.can_perform_update(old_event, new_event, calendar_group_id=calendar_group.id)
        is False
    )


@pytest.mark.django_db
def test_can_perform_update_accepts_exact_new_span_for_grouped_event(
    service, org, calendar, calendar_group, grouped_event
):
    calendar_group.duration = datetime.timedelta(minutes=30)
    calendar_group.save(update_fields=["duration"])

    token, _ = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_id=calendar.id,
        event_id=grouped_event.id,
    )
    service.token = token
    start = timezone.now()
    old_event = _event_data(grouped_event, start, datetime.timedelta(minutes=30))
    new_event = _event_data(
        grouped_event, start + datetime.timedelta(hours=1), datetime.timedelta(minutes=30)
    )

    assert (
        service.can_perform_update(old_event, new_event, calendar_group_id=calendar_group.id)
        is True
    )


@pytest.mark.django_db
def test_can_perform_update_cancellation_unaffected_by_group_duration(
    service, org, calendar, calendar_group, grouped_event
):
    """Cancellation (``new_event is None``) has no span to pin -- skipped entirely,
    even for a grouped event whose group pins a duration."""
    calendar_group.duration = datetime.timedelta(minutes=30)
    calendar_group.save(update_fields=["duration"])

    token, _ = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CANCEL],
        calendar_id=calendar.id,
        event_id=grouped_event.id,
    )
    service.token = token
    old_event = _event_data(grouped_event, timezone.now(), datetime.timedelta(minutes=30))

    assert service.can_perform_update(old_event, None, calendar_group_id=calendar_group.id) is True


@pytest.mark.django_db
def test_can_perform_update_non_grouped_event_carries_no_duration_pin(
    service, org, calendar, event
):
    """``calendar_group_id=None`` (the default, and the only value for a
    non-grouped event): no duration constraint at all, matching
    ``can_perform_scheduling``'s single-calendar behavior."""
    token, _ = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_id=calendar.id,
        event_id=event.id,
    )
    service.token = token
    start = timezone.now()
    old_event = _event_data(event, start, datetime.timedelta(minutes=30))
    new_event = _event_data(event, start + datetime.timedelta(hours=1), datetime.timedelta(hours=5))

    assert service.can_perform_update(old_event, new_event) is True


# ---------------------------------------------------------------------------
# can_perform_group_scheduling (times-aware overload)
# ---------------------------------------------------------------------------


@pytest.fixture
def group_with_member_calendar(org, calendar):
    grp = CalendarGroup.objects.create(organization=org, name="Pin Test Group")
    slot = CalendarGroupSlot.objects.create(organization=org, group=grp, name="Slot", order=0)
    CalendarGroupSlotMembership.objects.create(organization=org, slot=slot, calendar=calendar)
    return grp


@pytest.mark.django_db
def test_can_perform_group_scheduling_rejects_span_mismatch_when_pinned(
    service, org, group_with_member_calendar
):
    group_with_member_calendar.duration = datetime.timedelta(minutes=30)
    group_with_member_calendar.save(update_fields=["duration"])

    token, _ = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=group_with_member_calendar.id,
    )
    service.token = token
    start = timezone.now()

    result = service.can_perform_group_scheduling(
        group=group_with_member_calendar,
        start_time=start,
        end_time=start + datetime.timedelta(minutes=45),
    )
    assert result is False


@pytest.mark.django_db
def test_can_perform_group_scheduling_accepts_exact_span_when_pinned(
    service, org, group_with_member_calendar
):
    group_with_member_calendar.duration = datetime.timedelta(minutes=30)
    group_with_member_calendar.save(update_fields=["duration"])

    token, _ = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=group_with_member_calendar.id,
    )
    service.token = token
    start = timezone.now()

    result = service.can_perform_group_scheduling(
        group=group_with_member_calendar,
        start_time=start,
        end_time=start + datetime.timedelta(minutes=30),
    )
    assert result is True


@pytest.mark.django_db
def test_can_perform_group_scheduling_pin_enforced_even_with_public_group(service, org, calendar):
    """Ordering regression test.

    ``accepts_public_scheduling=True`` returns True at the TOP of
    ``can_perform_group_scheduling`` WITHOUT reading anything else. If the
    duration check were placed after that short-circuit (instead of before
    it), this test would fail: a pinned group would be silently unenforced
    on exactly the groups most likely to be publicly bookable. This is the
    regression test the plan calls out explicitly and it must not be
    dropped -- it moved here from the token-duration design with the same
    intent, now checking ``group.duration`` instead of ``token.duration``.
    """
    public_group = CalendarGroup.objects.create(
        organization=org,
        name="Public Group",
        accepts_public_scheduling=True,
        duration=datetime.timedelta(minutes=30),
    )
    slot = CalendarGroupSlot.objects.create(
        organization=org, group=public_group, name="Slot", order=0
    )
    CalendarGroupSlotMembership.objects.create(organization=org, slot=slot, calendar=calendar)

    token, _ = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=public_group.id,
    )
    service.token = token
    start = timezone.now()

    result = service.can_perform_group_scheduling(
        group=public_group,
        start_time=start,
        end_time=start + datetime.timedelta(minutes=45),
    )
    assert result is False


@pytest.mark.django_db
def test_can_perform_group_scheduling_without_times_skips_duration_check(
    service, org, group_with_member_calendar
):
    """Omitting ``start_time``/``end_time`` (pre-existing call sites) skips the
    duration check entirely -- this is the backward-compatibility contract for
    callers that only need the scope question answered."""
    group_with_member_calendar.duration = datetime.timedelta(minutes=30)
    group_with_member_calendar.save(update_fields=["duration"])

    token, _ = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=group_with_member_calendar.id,
    )
    service.token = token

    assert service.can_perform_group_scheduling(group=group_with_member_calendar) is True


@pytest.mark.django_db
def test_can_perform_group_scheduling_private_group_unpinned_unaffected(
    service, org, group_with_member_calendar
):
    """A private group (accepts_public_scheduling=False, the default) with
    duration=None is unaffected -- matching every group created before this
    field existed."""
    assert group_with_member_calendar.duration is None
    assert group_with_member_calendar.accepts_public_scheduling is False

    token, _ = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=group_with_member_calendar.id,
    )
    service.token = token
    start = timezone.now()

    result = service.can_perform_group_scheduling(
        group=group_with_member_calendar,
        start_time=start,
        end_time=start + datetime.timedelta(hours=5),
    )
    assert result is True


@pytest.mark.django_db
def test_can_perform_group_scheduling_fail_closed_public_group_no_duration(
    service, org, group_with_member_calendar
):
    """Fail-closed: a public group with duration=None refuses booking outright
    rather than allowing any length. This is the specific hole the codeless
    public-group booking path used to leave open under the old
    token-duration design: no code presented, no pin to check, any length
    accepted. A misconfigured public group must surface immediately here."""
    group_with_member_calendar.accepts_public_scheduling = True
    group_with_member_calendar.save(update_fields=["accepts_public_scheduling"])
    assert group_with_member_calendar.duration is None

    # No token at all -- the codeless public-group path.
    start = timezone.now()

    result = service.can_perform_group_scheduling(
        group=group_with_member_calendar,
        start_time=start,
        end_time=start + datetime.timedelta(minutes=30),
    )
    assert result is False
