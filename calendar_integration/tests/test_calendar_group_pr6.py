"""Tests for PR6 follow-ups: bulk-modification parity, batched
`find_bookable_slots`, and `CalendarPermissionService.can_manage_calendar_group`."""

from datetime import timedelta
from unittest.mock import Mock

from django.utils import timezone

import pytest

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AvailableTime,
    Calendar,
    CalendarEvent,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarOwnership,
)
from calendar_integration.permissions import CalendarGroupPermission
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.calendar_permission_service import (
    CalendarPermissionService,
)
from organizations.models import Organization, OrganizationMembership
from users.models import User


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Clinic Org", should_sync_rooms=False)


@pytest.fixture
def other_org(db):
    return Organization.objects.create(name="Other", should_sync_rooms=False)


@pytest.fixture
def managed_calendars(organization):
    calendars = {}
    for name, external in (
        ("Dr. A", "phys_a"),
        ("Dr. B", "phys_b"),
        ("Room 1", "room_1"),
    ):
        calendars[external] = Calendar.objects.create(
            organization=organization,
            name=name,
            external_id=external,
            provider=CalendarProvider.GOOGLE,
            calendar_type=(
                CalendarType.PERSONAL if external.startswith("phys_") else CalendarType.RESOURCE
            ),
            manage_available_windows=True,
        )
    return calendars


@pytest.fixture
def clinic_group(organization, managed_calendars):
    group = CalendarGroup.objects.create(organization=organization, name="Clinic")
    physicians = CalendarGroupSlot.objects.create(
        organization=organization, group=group, name="Physicians", order=0
    )
    rooms = CalendarGroupSlot.objects.create(
        organization=organization, group=group, name="Rooms", order=1
    )
    for cal in (managed_calendars["phys_a"], managed_calendars["phys_b"]):
        CalendarGroupSlotMembership.objects.create(
            organization=organization, slot=physicians, calendar=cal
        )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=rooms, calendar=managed_calendars["room_1"]
    )
    return group


@pytest.fixture
def service(organization):
    svc = CalendarGroupService()
    svc.initialize(organization=organization)
    return svc


def _seed_availability(calendars, start, end):
    for cal in calendars:
        AvailableTime.objects.create(
            organization=cal.organization,
            calendar=cal,
            start_time_tz_unaware=start,
            end_time_tz_unaware=end,
            timezone="UTC",
        )


# ---------------------------------------------------------------------------
# bulk-modification parity
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_only_groups_bookable_in_ranges_with_bulk_modifications_runs(
    organization, clinic_group, managed_calendars
):
    """Smoke test: the bulk-mods variant returns the same group as the non-bulk
    variant when no bulk modifications exist on events or blocked times."""
    now = timezone.now().replace(microsecond=0)
    start = now + timedelta(hours=1)
    end = start + timedelta(hours=1)
    _seed_availability(managed_calendars.values(), start, end)

    without = list(
        CalendarGroup.objects.filter_by_organization(
            organization.id
        ).only_groups_bookable_in_ranges([(start, end)])
    )
    with_bulk = list(
        CalendarGroup.objects.filter_by_organization(
            organization.id
        ).only_groups_bookable_in_ranges_with_bulk_modifications([(start, end)])
    )
    assert [g.id for g in without] == [g.id for g in with_bulk] == [clinic_group.id]


@pytest.mark.django_db
def test_check_group_availability_with_bulk_modifications_flag(
    service, clinic_group, managed_calendars
):
    now = timezone.now().replace(microsecond=0)
    start = now + timedelta(hours=1)
    end = start + timedelta(hours=1)
    _seed_availability(managed_calendars.values(), start, end)

    result_default = service.check_group_availability(
        group_id=clinic_group.id, ranges=[(start, end)]
    )
    result_bulk = service.check_group_availability(
        group_id=clinic_group.id,
        ranges=[(start, end)],
        with_bulk_modifications=True,
    )
    # Same shape/contents when no bulk modifications exist.
    assert [s.slot_id for s in result_default[0].slots] == [s.slot_id for s in result_bulk[0].slots]
    for default_slot, bulk_slot in zip(result_default[0].slots, result_bulk[0].slots, strict=False):
        assert default_slot.available_calendar_ids == bulk_slot.available_calendar_ids


# ---------------------------------------------------------------------------
# Batched `find_bookable_slots`
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_find_bookable_slots_batched_equals_previous_behavior(
    service, clinic_group, managed_calendars
):
    now = timezone.now().replace(microsecond=0)
    start = now + timedelta(hours=1)
    # Managed calendars need AvailableTime for each candidate window; make a
    # single span covering the whole search window.
    _seed_availability(managed_calendars.values(), start, start + timedelta(hours=2))
    proposals = service.find_bookable_slots(
        group_id=clinic_group.id,
        search_window_start=start,
        search_window_end=start + timedelta(hours=2),
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=30),
    )
    # 4 candidate windows fit within 2h at 30min step; all should be bookable.
    assert [(p.start_time, p.end_time) for p in proposals] == [
        (start + timedelta(minutes=i * 30), start + timedelta(minutes=i * 30 + 30))
        for i in range(4)
    ]


@pytest.mark.django_db
def test_find_bookable_slots_respects_unmanaged_blocking(
    service, organization, managed_calendars, clinic_group
):
    """Turn room_1 into an unmanaged calendar with a conflicting event mid-window.
    Candidate windows overlapping that event should be excluded, others kept."""
    room = managed_calendars["room_1"]
    room.manage_available_windows = False
    room.save(update_fields=["manage_available_windows"])

    # physicians remain managed and have availability across the whole window
    now = timezone.now().replace(microsecond=0)
    start = now + timedelta(hours=1)
    _seed_availability(
        [managed_calendars["phys_a"], managed_calendars["phys_b"]],
        start,
        start + timedelta(hours=2),
    )
    # Room's conflict lives in the 2nd 30-min slot.
    CalendarEvent.objects.create(
        organization=organization,
        calendar_fk=room,
        title="Room booked",
        external_id="ev_room",
        start_time_tz_unaware=start + timedelta(minutes=30),
        end_time_tz_unaware=start + timedelta(minutes=60),
        timezone="UTC",
    )

    proposals = service.find_bookable_slots(
        group_id=clinic_group.id,
        search_window_start=start,
        search_window_end=start + timedelta(hours=2),
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=30),
    )
    # Windows [0..30) and [60..90) and [90..120) are free;
    # [30..60) overlaps the room event → excluded.
    observed = [(p.start_time, p.end_time) for p in proposals]
    assert (start, start + timedelta(minutes=30)) in observed
    assert (start + timedelta(minutes=30), start + timedelta(minutes=60)) not in observed
    assert (start + timedelta(minutes=60), start + timedelta(minutes=90)) in observed


@pytest.mark.django_db
def test_find_bookable_slots_empty_when_a_slot_has_no_calendars_in_pool(service, organization):
    empty_group = CalendarGroup.objects.create(organization=organization, name="Empty")
    CalendarGroupSlot.objects.create(organization=organization, group=empty_group, name="Nobody")
    now = timezone.now().replace(microsecond=0)
    proposals = service.find_bookable_slots(
        group_id=empty_group.id,
        search_window_start=now,
        search_window_end=now + timedelta(hours=1),
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=30),
    )
    assert proposals == []


@pytest.mark.django_db
def test_find_bookable_slots_single_query_per_type(
    service, clinic_group, managed_calendars, django_assert_max_num_queries
):
    """The batched implementation should issue a bounded number of queries
    regardless of candidate count — the key win of the optimization. We
    scan a window with many candidates and assert the count stays small.
    """
    now = timezone.now().replace(microsecond=0)
    start = now + timedelta(hours=1)
    _seed_availability(managed_calendars.values(), start, start + timedelta(hours=6))
    # 6h at 15min step = 24 candidates. Pre-optimization this was ~24 round-trips.
    # The batched implementation should use a small constant count instead.
    with django_assert_max_num_queries(8):
        service.find_bookable_slots(
            group_id=clinic_group.id,
            search_window_start=start,
            search_window_end=start + timedelta(hours=6),
            duration=timedelta(minutes=30),
            slot_step=timedelta(minutes=15),
        )


# ---------------------------------------------------------------------------
# can_manage_calendar_group
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_can_manage_calendar_group_true_for_owner(organization, clinic_group, managed_calendars):
    owner = User.objects.create_user(email="owner@example.com")
    CalendarOwnership.objects.create(
        organization=organization, calendar=managed_calendars["phys_a"], user=owner
    )
    svc = CalendarPermissionService()
    assert svc.can_manage_calendar_group(user=owner, group=clinic_group) is True


@pytest.mark.django_db
def test_can_manage_calendar_group_false_for_non_owner(organization, clinic_group):
    stranger = User.objects.create_user(email="stranger@example.com")
    svc = CalendarPermissionService()
    assert svc.can_manage_calendar_group(user=stranger, group=clinic_group) is False


@pytest.mark.django_db
def test_can_manage_calendar_group_scoped_to_org(organization, other_org, clinic_group):
    # Owner of an *other-org* calendar doesn't pass.
    user = User.objects.create_user(email="xorg@example.com")
    other_calendar = Calendar.objects.create(
        organization=other_org, name="X", external_id="x", provider=CalendarProvider.INTERNAL
    )
    CalendarOwnership.objects.create(organization=other_org, calendar=other_calendar, user=user)
    svc = CalendarPermissionService()
    assert svc.can_manage_calendar_group(user=user, group=clinic_group) is False


# ---------------------------------------------------------------------------
# CalendarGroupPermission now delegates to CalendarPermissionService
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_calendar_group_permission_delegates_to_permission_service(
    organization, clinic_group, managed_calendars
):
    owner = User.objects.create_user(email="delegate@example.com")
    OrganizationMembership.objects.create(user=owner, organization=organization)
    CalendarOwnership.objects.create(
        organization=organization, calendar=managed_calendars["phys_a"], user=owner
    )

    perm = CalendarGroupPermission(calendar_permission_service=CalendarPermissionService())
    request = Mock()
    request.user = owner
    assert perm.has_permission(request, view=Mock()) is True
    assert perm.has_object_permission(request, view=Mock(), obj=clinic_group) is True


@pytest.mark.django_db
def test_calendar_group_permission_falls_back_when_service_missing(
    organization, clinic_group, managed_calendars
):
    """If DI fails to wire the service we don't crash — the permission falls
    back to the inline ownership check and still makes a correct decision."""
    owner = User.objects.create_user(email="fallback@example.com")
    OrganizationMembership.objects.create(user=owner, organization=organization)
    CalendarOwnership.objects.create(
        organization=organization, calendar=managed_calendars["phys_a"], user=owner
    )
    perm = CalendarGroupPermission(calendar_permission_service=None)
    request = Mock()
    request.user = owner
    assert perm.has_object_permission(request, view=Mock(), obj=clinic_group) is True
