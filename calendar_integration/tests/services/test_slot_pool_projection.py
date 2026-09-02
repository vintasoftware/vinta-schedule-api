"""Calendar Pools Phase 3: attaching a pool to a slot projects its roster.

A slot's bookable roster is the UNION of its inline calendars and the calendars
of every attached ``CalendarPool``, projected into
``CalendarGroupSlotMembership`` with ``source_pool`` naming the origin. These
tests pin the four properties that make the union safe:

1. Attaching makes the pool's calendars bookable; detaching removes exactly the
   rows that pool projected.
2. A calendar reachable from two sources survives losing one.
3. ``required_count`` counts distinct CALENDARS, not membership rows, so one
   calendar present twice never satisfies a slot needing two.
4. A group with no pools attached is byte-identical, query count included, to
   what it was before pools existed.
"""

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db.models import ProtectedError
from django.utils import timezone

import pytest

import calendar_integration.signals as signals_module
from audit_integration.constants import AuditAction
from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.exceptions import CalendarGroupValidationError
from calendar_integration.factories import create_calendar_pool, create_calendar_pool_membership
from calendar_integration.models import (
    AvailableTime,
    Calendar,
    CalendarGroup,
    CalendarGroupSlotMembership,
    CalendarGroupSlotPool,
    CalendarPool,
)
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.dataclasses import (
    CalendarGroupInputData,
    CalendarGroupSlotInputData,
    CalendarGroupSlotSelectionInputData,
)
from organizations.models import Organization


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Pool Projection Org", should_sync_rooms=False)


@pytest.fixture
def service(organization):
    svc = CalendarGroupService()
    svc.initialize(organization=organization)
    return svc


@pytest.fixture
def audit_service():
    from di_core.containers import get_container

    return get_container().audit_service()


@pytest.fixture
def audited_service(organization, audit_service):
    """`service`, but with a real `audit_service` bound -- for tests that
    inspect what gets audited rather than just what gets projected."""
    svc = CalendarGroupService(audit_service=audit_service)
    svc.initialize(organization=organization)
    return svc


@pytest.fixture
def calendars(organization):
    made = {}
    for name, external, kind in (
        ("Dr. A", "phys_a", CalendarType.PERSONAL),
        ("Dr. B", "phys_b", CalendarType.PERSONAL),
        ("Dr. C", "phys_c", CalendarType.PERSONAL),
        ("Room 1", "room_1", CalendarType.RESOURCE),
    ):
        made[external] = Calendar.objects.create(
            organization=organization,
            name=name,
            external_id=external,
            provider=CalendarProvider.GOOGLE,
            calendar_type=kind,
            manage_available_windows=True,
        )
    return made


@pytest.fixture
def nurses_pool(organization, calendars):
    return create_calendar_pool(
        organization=organization,
        name="Nurses",
        calendars=[calendars["phys_b"]],
    )


@pytest.fixture
def seniors_pool(organization, calendars):
    return create_calendar_pool(
        organization=organization,
        name="Senior Nurses",
        calendars=[calendars["phys_b"]],
    )


# Query count `update_group` issues for `_group_input`'s two-slot fixture when
# no slot in the payload sends `pool_ids` -- see
# test_update_group_on_a_no_pool_group_issues_no_calendar_pool_queries.
_NO_POOL_UPDATE_GROUP_QUERY_COUNT = 11


def _group_input(calendars, *, physician_calendar_ids, pool_ids=None, required_count=1):
    return CalendarGroupInputData(
        name="Clinic Appointments",
        description="",
        slots=[
            CalendarGroupSlotInputData(
                name="Physicians",
                calendar_ids=physician_calendar_ids,
                pool_ids=pool_ids,
                required_count=required_count,
                order=0,
            ),
            CalendarGroupSlotInputData(
                name="Rooms",
                calendar_ids=[calendars["room_1"].id],
                required_count=1,
                order=1,
            ),
        ],
    )


def _roster(organization, slot) -> set[int]:
    """The slot's resolved roster: distinct calendar ids, whatever the source."""
    return set(
        CalendarGroupSlotMembership.objects.filter_by_organization(organization.id)
        .filter(slot_fk=slot)
        .values_list("calendar_fk_id", flat=True)
    )


# ---------------------------------------------------------------------------
# Attach / detach
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_attaching_a_pool_makes_its_calendars_bookable_in_the_slot(
    service, organization, calendars, nurses_pool
):
    group = service.create_group(
        _group_input(calendars, physician_calendar_ids=[calendars["phys_a"].id])
    )
    physicians = group.slots.get(name="Physicians")
    assert _roster(organization, physicians) == {calendars["phys_a"].id}

    service.update_group(
        group.id,
        _group_input(
            calendars,
            physician_calendar_ids=[calendars["phys_a"].id],
            pool_ids=[nurses_pool.id],
        ),
    )

    physicians.refresh_from_db()
    assert _roster(organization, physicians) == {calendars["phys_a"].id, calendars["phys_b"].id}
    projected = CalendarGroupSlotMembership.objects.filter_by_organization(organization.id).filter(
        slot_fk=physicians, calendar_fk=calendars["phys_b"]
    )
    assert projected.count() == 1
    assert projected.get().source_pool_fk_id == nurses_pool.id
    assert (
        CalendarGroupSlotPool.objects.filter_by_organization(organization.id)
        .filter(slot_fk=physicians, pool_fk=nurses_pool)
        .exists()
    )


@pytest.mark.django_db
def test_detaching_a_pool_removes_its_projected_calendars(
    service, organization, calendars, nurses_pool
):
    group = service.create_group(
        _group_input(
            calendars,
            physician_calendar_ids=[calendars["phys_a"].id],
            pool_ids=[nurses_pool.id],
        )
    )
    physicians = group.slots.get(name="Physicians")
    assert _roster(organization, physicians) == {calendars["phys_a"].id, calendars["phys_b"].id}

    service.update_group(
        group.id,
        _group_input(calendars, physician_calendar_ids=[calendars["phys_a"].id], pool_ids=[]),
    )

    assert _roster(organization, physicians) == {calendars["phys_a"].id}
    assert not CalendarGroupSlotPool.objects.filter_by_organization(organization.id).exists()


@pytest.mark.django_db
def test_omitted_pool_ids_leaves_attachments_unchanged(
    service, organization, calendars, nurses_pool
):
    """The omit-versus-empty-list distinction: `None` is not `[]`."""
    group = service.create_group(
        _group_input(
            calendars,
            physician_calendar_ids=[calendars["phys_a"].id],
            pool_ids=[nurses_pool.id],
        )
    )
    physicians = group.slots.get(name="Physicians")

    # pool_ids omitted entirely -- the shape every pre-pools client sends.
    service.update_group(
        group.id,
        _group_input(calendars, physician_calendar_ids=[calendars["phys_a"].id]),
    )

    assert _roster(organization, physicians) == {calendars["phys_a"].id, calendars["phys_b"].id}
    assert (
        CalendarGroupSlotPool.objects.filter_by_organization(organization.id)
        .filter(slot_fk=physicians)
        .count()
        == 1
    )


# ---------------------------------------------------------------------------
# Survival across sources -- the whole point of the union
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_calendar_inline_and_in_pool_survives_the_pool_being_detached(
    service, organization, calendars, nurses_pool
):
    group = service.create_group(
        _group_input(
            calendars,
            # phys_b is BOTH inline and on the Nurses roster.
            physician_calendar_ids=[calendars["phys_a"].id, calendars["phys_b"].id],
            pool_ids=[nurses_pool.id],
        )
    )
    physicians = group.slots.get(name="Physicians")
    rows = CalendarGroupSlotMembership.objects.filter_by_organization(organization.id).filter(
        slot_fk=physicians, calendar_fk=calendars["phys_b"]
    )
    assert rows.count() == 2
    assert {row.source_pool_fk_id for row in rows} == {None, nurses_pool.id}

    service.update_group(
        group.id,
        _group_input(
            calendars,
            physician_calendar_ids=[calendars["phys_a"].id, calendars["phys_b"].id],
            pool_ids=[],
        ),
    )

    assert _roster(organization, physicians) == {calendars["phys_a"].id, calendars["phys_b"].id}
    surviving = CalendarGroupSlotMembership.objects.filter_by_organization(organization.id).filter(
        slot_fk=physicians, calendar_fk=calendars["phys_b"]
    )
    assert surviving.count() == 1
    assert surviving.get().source_pool_fk_id is None


@pytest.mark.django_db
def test_calendar_in_two_pools_survives_one_being_detached(
    service, organization, calendars, nurses_pool, seniors_pool
):
    group = service.create_group(
        _group_input(
            calendars,
            physician_calendar_ids=[calendars["phys_a"].id],
            pool_ids=[nurses_pool.id, seniors_pool.id],
        )
    )
    physicians = group.slots.get(name="Physicians")
    rows = CalendarGroupSlotMembership.objects.filter_by_organization(organization.id).filter(
        slot_fk=physicians, calendar_fk=calendars["phys_b"]
    )
    assert {row.source_pool_fk_id for row in rows} == {nurses_pool.id, seniors_pool.id}

    service.update_group(
        group.id,
        _group_input(
            calendars,
            physician_calendar_ids=[calendars["phys_a"].id],
            pool_ids=[seniors_pool.id],
        ),
    )

    assert _roster(organization, physicians) == {calendars["phys_a"].id, calendars["phys_b"].id}
    surviving = CalendarGroupSlotMembership.objects.filter_by_organization(organization.id).filter(
        slot_fk=physicians, calendar_fk=calendars["phys_b"]
    )
    assert surviving.count() == 1
    assert surviving.get().source_pool_fk_id == seniors_pool.id


@pytest.mark.django_db
def test_removing_an_inline_calendar_does_not_touch_the_projected_row(
    service, organization, calendars, nurses_pool
):
    """The inline path must not reach across into projected rows either."""
    group = service.create_group(
        _group_input(
            calendars,
            physician_calendar_ids=[calendars["phys_a"].id, calendars["phys_b"].id],
            pool_ids=[nurses_pool.id],
        )
    )
    physicians = group.slots.get(name="Physicians")

    # Drop phys_b from the INLINE roster; the pool still lists it.
    service.update_group(
        group.id,
        _group_input(
            calendars,
            physician_calendar_ids=[calendars["phys_a"].id],
            pool_ids=[nurses_pool.id],
        ),
    )

    remaining = CalendarGroupSlotMembership.objects.filter_by_organization(organization.id).filter(
        slot_fk=physicians, calendar_fk=calendars["phys_b"]
    )
    assert remaining.count() == 1
    assert remaining.get().source_pool_fk_id == nurses_pool.id
    assert _roster(organization, physicians) == {calendars["phys_a"].id, calendars["phys_b"].id}


# ---------------------------------------------------------------------------
# Deletion semantics
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_deleting_a_slot_removes_its_attachments_and_projected_rows(
    service, organization, calendars, nurses_pool
):
    group = service.create_group(
        _group_input(
            calendars,
            physician_calendar_ids=[calendars["phys_a"].id],
            pool_ids=[nurses_pool.id],
        )
    )
    physicians = group.slots.get(name="Physicians")

    physicians.delete()

    assert not CalendarGroupSlotPool.objects.filter_by_organization(organization.id).exists()
    assert (
        not CalendarGroupSlotMembership.objects.filter_by_organization(organization.id)
        .filter(slot_fk_id=physicians.id)
        .exists()
    )
    # The pool itself is untouched -- slot deletion drops the attachment, not
    # the roster it pointed at.
    assert CalendarPool.objects.filter_by_organization(organization.id).count() == 1
    assert nurses_pool.memberships.count() == 1


@pytest.mark.django_db
def test_deleting_a_referenced_pool_is_refused(service, organization, calendars, nurses_pool):
    """PROTECT on ``CalendarGroupSlotPool.pool`` is the schema-level refusal."""
    service.create_group(
        _group_input(
            calendars,
            physician_calendar_ids=[calendars["phys_a"].id],
            pool_ids=[nurses_pool.id],
        )
    )

    with pytest.raises(ProtectedError):
        nurses_pool.delete()

    assert (
        CalendarPool.objects.filter_by_organization(organization.id)
        .filter(id=nurses_pool.id)
        .exists()
    )


@pytest.mark.django_db
def test_deleting_an_unreferenced_pool_succeeds(service, organization, calendars, nurses_pool):
    group = service.create_group(
        _group_input(
            calendars,
            physician_calendar_ids=[calendars["phys_a"].id],
            pool_ids=[nurses_pool.id],
        )
    )
    service.update_group(
        group.id,
        _group_input(calendars, physician_calendar_ids=[calendars["phys_a"].id], pool_ids=[]),
    )

    nurses_pool.delete()

    assert not CalendarPool.objects.filter_by_organization(organization.id).exists()


# ---------------------------------------------------------------------------
# required_count under duplicate calendars -- the Count regression
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_required_count_two_is_not_satisfied_by_one_calendar_from_two_sources(
    service, organization, calendars
):
    """The reason the ``Count`` fix ships in this phase.

    ``Count("memberships", distinct=True)`` counts distinct membership ROWS. A
    slot needing two calendars, holding exactly one calendar that is both inline
    and projected from a pool, has two rows and would be reported bookable --
    with no second calendar to book.
    """
    solo_pool = create_calendar_pool(
        organization=organization,
        name="Solo",
        calendars=[calendars["phys_a"]],
    )
    now = timezone.now().replace(microsecond=0)
    window = (now + timedelta(hours=1), now + timedelta(hours=2))
    for calendar in (calendars["phys_a"], calendars["room_1"]):
        AvailableTime.objects.create(
            organization=organization,
            calendar=calendar,
            start_time_tz_unaware=window[0],
            end_time_tz_unaware=window[1],
            timezone="UTC",
        )

    group = service.create_group(
        _group_input(
            calendars,
            # phys_a inline AND on the Solo roster; phys_c pads the roster so
            # required_count=2 passes input validation.
            physician_calendar_ids=[calendars["phys_a"].id, calendars["phys_c"].id],
            pool_ids=[solo_pool.id],
            required_count=2,
        )
    )
    physicians = group.slots.get(name="Physicians")
    # Two rows, one calendar -- exactly the shape that fools a row count.
    assert (
        CalendarGroupSlotMembership.objects.filter_by_organization(organization.id)
        .filter(slot_fk=physicians, calendar_fk=calendars["phys_a"])
        .count()
        == 2
    )

    bookable = list(
        CalendarGroup.objects.filter_by_organization(
            organization.id
        ).only_groups_bookable_in_ranges([window])
    )
    assert bookable == []


@pytest.mark.django_db
def test_required_count_two_is_satisfied_by_two_distinct_available_calendars(
    service, organization, calendars, nurses_pool
):
    """The positive control for the test above: two real calendars do satisfy it."""
    now = timezone.now().replace(microsecond=0)
    window = (now + timedelta(hours=1), now + timedelta(hours=2))
    for calendar in (calendars["phys_a"], calendars["phys_b"], calendars["room_1"]):
        AvailableTime.objects.create(
            organization=organization,
            calendar=calendar,
            start_time_tz_unaware=window[0],
            end_time_tz_unaware=window[1],
            timezone="UTC",
        )

    group = service.create_group(
        _group_input(
            calendars,
            physician_calendar_ids=[calendars["phys_a"].id],
            pool_ids=[nurses_pool.id],
            required_count=2,
        )
    )

    bookable = list(
        CalendarGroup.objects.filter_by_organization(
            organization.id
        ).only_groups_bookable_in_ranges([window])
    )
    assert bookable == [group]


@pytest.mark.django_db
def test_required_count_may_be_satisfied_entirely_by_pool_calendars(
    service, organization, calendars, nurses_pool
):
    """A slot may be made of pool calendars alone -- no inline calendar required."""
    group = service.create_group(
        _group_input(calendars, physician_calendar_ids=[], pool_ids=[nurses_pool.id])
    )
    physicians = group.slots.get(name="Physicians")
    assert _roster(organization, physicians) == {calendars["phys_b"].id}


@pytest.mark.django_db
def test_required_count_above_effective_roster_size_is_rejected(
    service, organization, calendars, nurses_pool
):
    with pytest.raises(CalendarGroupValidationError, match="exceeds pool size"):
        service.create_group(
            _group_input(
                calendars,
                physician_calendar_ids=[calendars["phys_a"].id],
                pool_ids=[nurses_pool.id],
                required_count=3,
            )
        )


@pytest.mark.django_db
def test_pool_from_another_organization_is_rejected(service, calendars, db):
    other_org = Organization.objects.create(name="Other Org", should_sync_rooms=False)
    foreign_pool = CalendarPool.objects.create(organization=other_org, name="Theirs")

    with pytest.raises(CalendarGroupValidationError, match="do not belong to this organization"):
        service.create_group(
            _group_input(
                calendars,
                physician_calendar_ids=[calendars["phys_a"].id],
                pool_ids=[foreign_pool.id],
            )
        )


@pytest.mark.django_db
def test_one_pool_attached_to_two_slots_of_a_group_is_rejected(service, calendars, nurses_pool):
    """The one-calendar-per-slot rule is judged on the effective roster."""
    data = _group_input(
        calendars,
        physician_calendar_ids=[calendars["phys_a"].id],
        pool_ids=[nurses_pool.id],
    )
    data.slots[1].pool_ids = [nurses_pool.id]

    with pytest.raises(CalendarGroupValidationError, match="appears in multiple slots"):
        service.create_group(data)


# ---------------------------------------------------------------------------
# Self-gating: a group with no pools is unchanged
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_group_with_no_pools_has_no_projected_rows_and_no_attachments(
    service, organization, calendars
):
    group = service.create_group(
        _group_input(
            calendars,
            physician_calendar_ids=[calendars["phys_a"].id, calendars["phys_b"].id],
        )
    )
    physicians = group.slots.get(name="Physicians")

    assert _roster(organization, physicians) == {calendars["phys_a"].id, calendars["phys_b"].id}
    assert (
        CalendarGroupSlotMembership.objects.filter_by_organization(organization.id)
        .projected()
        .count()
        == 0
    )
    assert not CalendarGroupSlotPool.objects.filter_by_organization(organization.id).exists()


@pytest.mark.django_db
def test_no_pools_bookable_slot_query_count_is_unchanged(
    service, organization, calendars, django_assert_num_queries
):
    """A group with no pools issues exactly one query for the bookable check.

    ``only_groups_bookable_in_ranges`` is a single correlated query before and
    after this phase -- the ``Count`` fix changes the counted expression, not the
    query shape, and the projection adds no read.
    """
    now = timezone.now().replace(microsecond=0)
    window = (now + timedelta(hours=1), now + timedelta(hours=2))
    for calendar in (calendars["phys_a"], calendars["room_1"]):
        AvailableTime.objects.create(
            organization=organization,
            calendar=calendar,
            start_time_tz_unaware=window[0],
            end_time_tz_unaware=window[1],
            timezone="UTC",
        )
    group = service.create_group(
        _group_input(calendars, physician_calendar_ids=[calendars["phys_a"].id])
    )

    with django_assert_num_queries(1):
        bookable = list(
            CalendarGroup.objects.filter_by_organization(
                organization.id
            ).only_groups_bookable_in_ranges([window])
        )
    assert bookable == [group]


@pytest.mark.django_db
def test_no_pools_availability_output_is_unchanged(service, organization, calendars):
    """``check_group_availability`` reports the same rosters with pools in the schema."""
    now = timezone.now().replace(microsecond=0)
    window = (now + timedelta(hours=1), now + timedelta(hours=2))
    for calendar in (calendars["phys_a"], calendars["phys_b"], calendars["room_1"]):
        AvailableTime.objects.create(
            organization=organization,
            calendar=calendar,
            start_time_tz_unaware=window[0],
            end_time_tz_unaware=window[1],
            timezone="UTC",
        )
    group = service.create_group(
        _group_input(
            calendars,
            physician_calendar_ids=[calendars["phys_a"].id, calendars["phys_b"].id],
        )
    )

    availability = service.check_group_availability(group.id, [window])

    assert len(availability) == 1
    by_slot_name = {
        group.slots.get(id=slot.slot_id).name: sorted(slot.available_calendar_ids)
        for slot in availability[0].slots
    }
    assert by_slot_name == {
        "Physicians": sorted([calendars["phys_a"].id, calendars["phys_b"].id]),
        "Rooms": [calendars["room_1"].id],
    }


@pytest.mark.django_db
def test_availability_reports_each_calendar_once_when_reachable_twice(
    service, organization, calendars, nurses_pool
):
    """Duplicate membership rows must not duplicate a calendar in availability."""
    now = timezone.now().replace(microsecond=0)
    window = (now + timedelta(hours=1), now + timedelta(hours=2))
    for calendar in (calendars["phys_b"], calendars["room_1"]):
        AvailableTime.objects.create(
            organization=organization,
            calendar=calendar,
            start_time_tz_unaware=window[0],
            end_time_tz_unaware=window[1],
            timezone="UTC",
        )
    group = service.create_group(
        _group_input(
            calendars,
            physician_calendar_ids=[calendars["phys_b"].id],
            pool_ids=[nurses_pool.id],
        )
    )
    physicians = group.slots.get(name="Physicians")

    availability = service.check_group_availability(group.id, [window])

    physicians_slot = next(s for s in availability[0].slots if s.slot_id == physicians.id)
    assert physicians_slot.available_calendar_ids == [calendars["phys_b"].id]


# ---------------------------------------------------------------------------
# Group-scoped writes still resolve when a calendar has several roster rows
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_group_scoped_membership_resolves_when_a_calendar_has_several_roster_rows(
    service, organization, calendars, nurses_pool
):
    """Every group-scoped write (window / block / quota rule) funnels through
    ``_resolve_group_scoped_membership``, which used ``get()``. Once a calendar
    can hold both an inline and a projected row for one slot, ``get()`` raises
    ``MultipleObjectsReturned`` -- a 500 on an otherwise valid write.
    """
    group = service.create_group(
        _group_input(
            calendars,
            physician_calendar_ids=[calendars["phys_b"].id],
            pool_ids=[nurses_pool.id],
        )
    )
    physicians = group.slots.get(name="Physicians")
    assert (
        CalendarGroupSlotMembership.objects.filter_by_organization(organization.id)
        .filter(slot_fk=physicians, calendar_fk=calendars["phys_b"])
        .count()
        == 2
    )

    membership = service._resolve_group_scoped_membership(  # noqa: SLF001
        physicians.id, calendars["phys_b"].id
    )

    assert membership.slot_fk_id == physicians.id
    assert membership.calendar_fk_id == calendars["phys_b"].id


# ---------------------------------------------------------------------------
# Drift closed: a direct pool-roster edit reprojects immediately (BLOCKER)
#
# Everything above exercises `_reconcile_slot_pools` through the attach/detach
# path (`CalendarGroupService.update_group`). These tests instead edit
# `CalendarPoolMembership` directly -- what the admin inline, a shell session,
# or a data migration does -- and pin that the slot's projection reacts
# without anyone calling `_reconcile_slot_pools` themselves.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_removing_a_calendar_from_an_attached_pool_reprojects_immediately(
    service, organization, calendars, nurses_pool
):
    """The finding's exact scenario: dropping a calendar from a pool attached
    to a slot must make it immediately non-bookable through that slot, even
    though nobody called `update_group`."""
    group = service.create_group(
        _group_input(
            calendars,
            physician_calendar_ids=[calendars["phys_a"].id],
            pool_ids=[nurses_pool.id],
        )
    )
    physicians = group.slots.get(name="Physicians")
    assert _roster(organization, physicians) == {calendars["phys_a"].id, calendars["phys_b"].id}

    # What the admin inline does: delete the CalendarPoolMembership row
    # directly, never touching CalendarGroupSlotMembership or the slot.
    nurses_pool.memberships.get(calendar_fk=calendars["phys_b"]).delete()

    assert _roster(organization, physicians) == {calendars["phys_a"].id}
    # A brand-new booking against the calendar that just left the roster is
    # rejected -- the projected row cannot still be there to let it through.
    with pytest.raises(CalendarGroupValidationError, match="not in the pool"):
        service._validate_selections(  # noqa: SLF001
            group,
            [physicians],
            [
                CalendarGroupSlotSelectionInputData(
                    slot_id=physicians.id, calendar_ids=[calendars["phys_b"].id]
                )
            ],
        )


@pytest.mark.django_db
def test_adding_a_calendar_to_an_attached_pool_reprojects_immediately(
    service, organization, calendars, nurses_pool
):
    """The mirror case: a calendar added to an already-attached pool must be
    immediately bookable through every slot that pool is attached to."""
    group = service.create_group(
        _group_input(
            calendars,
            physician_calendar_ids=[calendars["phys_a"].id],
            pool_ids=[nurses_pool.id],
        )
    )
    physicians = group.slots.get(name="Physicians")
    assert calendars["phys_c"].id not in _roster(organization, physicians)

    create_calendar_pool_membership(
        organization=organization, pool=nurses_pool, calendar=calendars["phys_c"]
    )

    assert _roster(organization, physicians) == {
        calendars["phys_a"].id,
        calendars["phys_b"].id,
        calendars["phys_c"].id,
    }
    selections = service._validate_selections(  # noqa: SLF001
        group,
        [physicians],
        [
            CalendarGroupSlotSelectionInputData(
                slot_id=physicians.id, calendar_ids=[calendars["phys_c"].id]
            )
        ],
    )
    assert selections[physicians.id].calendar_ids == [calendars["phys_c"].id]


@pytest.mark.django_db
def test_pool_roster_edit_reprojection_leaves_inline_rows_untouched(
    service, organization, calendars, nurses_pool
):
    service.create_group(
        _group_input(
            calendars,
            physician_calendar_ids=[calendars["phys_a"].id],
            pool_ids=[nurses_pool.id],
        )
    )
    inline_ids_before = set(
        CalendarGroupSlotMembership.objects.filter_by_organization(organization.id)
        .inline()
        .values_list("id", flat=True)
    )

    nurses_pool.memberships.get(calendar_fk=calendars["phys_b"]).delete()
    create_calendar_pool_membership(
        organization=organization, pool=nurses_pool, calendar=calendars["phys_c"]
    )

    assert (
        set(
            CalendarGroupSlotMembership.objects.filter_by_organization(organization.id)
            .inline()
            .values_list("id", flat=True)
        )
        == inline_ids_before
    )


@pytest.mark.django_db
def test_direct_pool_roster_edit_reports_no_drift(organization, service, calendars, nurses_pool):
    """The regression test for the closed hole: the drift sweep finds nothing
    to repair after a direct pool-roster edit, because the signal already
    reconciled it -- before this fix, this would have reported drift."""
    service.create_group(
        _group_input(
            calendars,
            physician_calendar_ids=[calendars["phys_a"].id],
            pool_ids=[nurses_pool.id],
        )
    )

    nurses_pool.memberships.get(calendar_fk=calendars["phys_b"]).delete()
    create_calendar_pool_membership(
        organization=organization, pool=nurses_pool, calendar=calendars["phys_c"]
    )

    out = StringIO()
    call_command("reconcile_calendar_pool_projections", stdout=out)

    assert "no drift found" in out.getvalue()
    assert "DRIFT DETECTED" not in out.getvalue()


@pytest.mark.django_db
def test_bulk_pool_roster_delete_reconciles_each_pool_once(
    service, organization, calendars, nurses_pool, monkeypatch
):
    """`pool.memberships.all().delete()` removes M rows through one queryset
    delete; Django's deletion collector still sends `post_delete` once per
    row, but the affected slots must be reconciled once for the whole
    operation, not once per deleted row."""
    service.create_group(
        _group_input(
            calendars,
            physician_calendar_ids=[calendars["phys_a"].id],
            pool_ids=[nurses_pool.id],
        )
    )
    create_calendar_pool_membership(
        organization=organization, pool=nurses_pool, calendar=calendars["phys_c"]
    )
    assert nurses_pool.memberships.count() == 2

    calls: list[tuple[frozenset[int], int]] = []
    original_reconcile_pools = signals_module.reconcile_pools

    def _tracking_reconcile(pool_ids, organization_id):
        calls.append((frozenset(pool_ids), organization_id))
        return original_reconcile_pools(pool_ids, organization_id)

    monkeypatch.setattr(signals_module, "reconcile_pools", _tracking_reconcile)

    nurses_pool.memberships.all().delete()

    assert calls == [(frozenset({nurses_pool.id}), organization.id)]


# ---------------------------------------------------------------------------
# Validation is scoped to what the caller submits (SHOULD-FIX #2)
#
# `_validate_slots_input` used to re-resolve a slot's UNCHANGED pool
# attachment (`pool_ids=None`) from the live database on every call, which
# meant a third party mutating a pool could make an unrelated `update_group`
# call fail -- naming a slot the caller never touched. These tests pin the
# two repro scenarios from the finding and that strict validation still
# applies where there is nothing persisted to trust.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_update_omitting_pool_ids_survives_a_third_party_pool_overlap(
    service, organization, calendars
):
    """Group G: slot A <- pool P {phys_a}, slot B <- pool Q {phys_b}. A third
    party adds phys_a to pool Q too. `update_group` on G that only changes the
    description, sending `pool_ids=None` for both slots, must still succeed --
    it must not re-derive the (now overlapping) rosters of pools it was never
    told about."""
    pool_p = create_calendar_pool(
        organization=organization, name="P", calendars=[calendars["phys_a"]]
    )
    pool_q = create_calendar_pool(
        organization=organization, name="Q", calendars=[calendars["phys_b"]]
    )
    group = service.create_group(
        CalendarGroupInputData(
            name="Clinic",
            description="",
            slots=[
                CalendarGroupSlotInputData(
                    name="Slot A", calendar_ids=[], pool_ids=[pool_p.id], required_count=1, order=0
                ),
                CalendarGroupSlotInputData(
                    name="Slot B", calendar_ids=[], pool_ids=[pool_q.id], required_count=1, order=1
                ),
            ],
        )
    )

    # phys_a is already on P (slot A); adding it to Q too creates an overlap
    # that only matters if this update re-resolves omitted pool_ids.
    create_calendar_pool_membership(
        organization=organization, pool=pool_q, calendar=calendars["phys_a"]
    )

    service.update_group(
        group.id,
        CalendarGroupInputData(
            name="Clinic",
            description="updated",
            slots=[
                CalendarGroupSlotInputData(
                    name="Slot A", calendar_ids=[], required_count=1, order=0
                ),
                CalendarGroupSlotInputData(
                    name="Slot B", calendar_ids=[], required_count=1, order=1
                ),
            ],
        ),
    )

    group.refresh_from_db()
    assert group.description == "updated"


@pytest.mark.django_db
def test_update_omitting_pool_ids_survives_a_shrunk_pool_below_required_count(
    service, organization, calendars, nurses_pool
):
    """Slot S: calendar_ids=[], required_count=2, pool P {phys_b, phys_c}. A
    third party removes phys_c from P. `update_group` that only changes the
    description, sending `pool_ids=None` for S, must still succeed."""
    create_calendar_pool_membership(
        organization=organization, pool=nurses_pool, calendar=calendars["phys_c"]
    )
    group = service.create_group(
        CalendarGroupInputData(
            name="Clinic",
            description="",
            slots=[
                CalendarGroupSlotInputData(
                    name="Slot S",
                    calendar_ids=[],
                    pool_ids=[nurses_pool.id],
                    required_count=2,
                    order=0,
                ),
            ],
        )
    )

    nurses_pool.memberships.get(calendar_fk=calendars["phys_c"]).delete()

    service.update_group(
        group.id,
        CalendarGroupInputData(
            name="Clinic",
            description="updated",
            slots=[
                CalendarGroupSlotInputData(
                    name="Slot S", calendar_ids=[], required_count=2, order=0
                ),
            ],
        ),
    )

    group.refresh_from_db()
    assert group.description == "updated"


@pytest.mark.django_db
def test_a_new_slot_with_no_calendars_and_no_pool_ids_is_still_rejected(service):
    """The trust only covers an EXISTING slot's omitted attachment; a
    brand-new slot (a create, or a name new to this update) has nothing
    persisted to trust and is still validated strictly."""
    with pytest.raises(CalendarGroupValidationError, match="must include at least one calendar"):
        service.create_group(
            CalendarGroupInputData(
                name="Clinic",
                description="",
                slots=[
                    CalendarGroupSlotInputData(
                        name="Empty", calendar_ids=[], required_count=1, order=0
                    ),
                ],
            )
        )


@pytest.mark.django_db
def test_update_group_on_a_no_pool_group_issues_no_calendar_pool_queries(
    service, organization, calendars, django_assert_num_queries
):
    """The related finding: `_resolve_effective_pool_ids` used to fire a
    `CalendarGroupSlotPool` query on every `update_group` call, even for a
    group that never had a pool. Restricting validation to explicitly
    submitted `pool_ids` removes it entirely for a payload that never sends
    one -- pinned as a total query count for `update_group` on this fixture,
    so a regression (pool-related or otherwise) shows up as a query-count
    failure, not just a slow one.
    """
    group = service.create_group(
        _group_input(calendars, physician_calendar_ids=[calendars["phys_a"].id])
    )

    with django_assert_num_queries(_NO_POOL_UPDATE_GROUP_QUERY_COUNT) as captured:
        service.update_group(
            group.id,
            _group_input(calendars, physician_calendar_ids=[calendars["phys_a"].id]),
        )

    pool_queries = [
        q["sql"]
        for q in captured.captured_queries
        if "calendargroupslotpool" in q["sql"].lower() or "calendarpool" in q["sql"].lower()
    ]
    assert pool_queries == []


# ---------------------------------------------------------------------------
# No spurious UPDATE audit on a slot created microseconds earlier (SHOULD-FIX #8)
# ---------------------------------------------------------------------------


def _slot_update_audit_payloads(mock_task, physicians_id):
    payloads = [call.args[0] for call in mock_task.delay.call_args_list]
    return [
        p
        for p in payloads
        if p["action_key"] == AuditAction.UPDATE
        and p["subject"]["subject_type"] == "calendar_integration.calendargroupslot"
        and p["subject"]["subject_id"] == str(physicians_id)
    ]


@pytest.mark.django_db
def test_create_group_with_pools_emits_no_slot_update_audit(
    audited_service, calendars, nurses_pool, django_capture_on_commit_callbacks
):
    """Attaching a pool inside `_create_slots` must not audit an UPDATE on a
    slot that was itself created in the same call -- an audit reader must not
    see an UPDATE nested inside the group's own CREATE."""
    with patch("vinta_audit_logs.tasks.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            group = audited_service.create_group(
                _group_input(
                    calendars,
                    physician_calendar_ids=[calendars["phys_a"].id],
                    pool_ids=[nurses_pool.id],
                )
            )

    physicians = group.slots.get(name="Physicians")
    assert _slot_update_audit_payloads(mock_task, physicians.id) == []


@pytest.mark.django_db
def test_update_group_attaching_a_pool_still_emits_slot_update_audit(
    audited_service, calendars, nurses_pool, django_capture_on_commit_callbacks
):
    """The audit skip is scoped to slot creation only -- attaching a pool to an
    already-existing slot through `update_group` still audits the change."""
    group = audited_service.create_group(
        _group_input(calendars, physician_calendar_ids=[calendars["phys_a"].id])
    )
    physicians = group.slots.get(name="Physicians")

    with patch("vinta_audit_logs.tasks.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            audited_service.update_group(
                group.id,
                _group_input(
                    calendars,
                    physician_calendar_ids=[calendars["phys_a"].id],
                    pool_ids=[nurses_pool.id],
                ),
            )

    slot_updates = _slot_update_audit_payloads(mock_task, physicians.id)
    assert len(slot_updates) == 1
    assert slot_updates[0]["diff"]["pool_ids"]["new"] == [nurses_pool.id]
