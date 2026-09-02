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

from django.utils import timezone

import pytest

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.exceptions import CalendarGroupValidationError
from calendar_integration.factories import create_calendar_pool
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
    from django.db.models import ProtectedError

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
