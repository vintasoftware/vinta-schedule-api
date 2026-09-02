"""Calendar Pools Phase 3: the uniqueness contract after the constraint swap.

``calendargroupslotmembership_unique_slot_calendar`` on ``(slot_fk,
calendar_fk)`` was replaced, because the projected union deliberately allows one
calendar to reach a slot from several sources. The replacement has to behave as
"unique on ``(slot_fk, calendar_fk, source_pool_fk)`` **with NULL treated as a
value**".

A plain three-column ``UniqueConstraint`` would pass a happy-path test and still
be wrong: Postgres treats NULLs as distinct, so it would accept two INLINE rows
for the same ``(slot, calendar)`` and silently double-count that calendar
towards ``required_count``. The tests below therefore lead with the rejection
case; the acceptance cases alone would not distinguish a correct constraint from
a broken one.

The mechanism is a pair of PARTIAL unique indexes rather than
``NULLS NOT DISTINCT`` (Postgres 15+) because local development runs Postgres 14.
"""

from django.db import IntegrityError, transaction

import pytest

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.factories import create_calendar_pool
from calendar_integration.models import (
    Calendar,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
)
from organizations.models import Organization


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Constraint Swap Org", should_sync_rooms=False)


@pytest.fixture
def calendar(organization):
    return Calendar.objects.create(
        organization=organization,
        name="Dr. A",
        external_id="phys_a",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
    )


@pytest.fixture
def slot(organization):
    group = CalendarGroup.objects.create(organization=organization, name="Clinic")
    return CalendarGroupSlot.objects.create(
        organization=organization,
        group=group,
        name="Physicians",
        order=0,
    )


@pytest.fixture
def pool(organization, calendar):
    return create_calendar_pool(organization=organization, name="Nurses", calendars=[calendar])


@pytest.fixture
def other_pool(organization, calendar):
    return create_calendar_pool(
        organization=organization, name="Senior Nurses", calendars=[calendar]
    )


def _membership(organization, slot, calendar, source_pool=None):
    return CalendarGroupSlotMembership.objects.create(
        organization=organization,
        slot=slot,
        calendar=calendar,
        source_pool=source_pool,
    )


# ---------------------------------------------------------------------------
# The NULL-semantics trap
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_two_inline_rows_for_the_same_slot_calendar_are_rejected(organization, slot, calendar):
    """The case a plain three-column unique constraint would wrongly accept."""
    _membership(organization, slot, calendar)

    with pytest.raises(IntegrityError), transaction.atomic():
        _membership(organization, slot, calendar)


@pytest.mark.django_db(transaction=True)
def test_two_rows_from_the_same_pool_for_the_same_slot_calendar_are_rejected(
    organization, slot, calendar, pool
):
    """The projected half of the constraint still forbids exact duplicates."""
    _membership(organization, slot, calendar, source_pool=pool)

    with pytest.raises(IntegrityError), transaction.atomic():
        _membership(organization, slot, calendar, source_pool=pool)


# ---------------------------------------------------------------------------
# What the swap deliberately allows
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_inline_plus_projected_row_for_the_same_slot_calendar_is_accepted(
    organization, slot, calendar, pool
):
    """The union: one calendar, two sources, two rows."""
    inline = _membership(organization, slot, calendar)
    projected = _membership(organization, slot, calendar, source_pool=pool)

    rows = CalendarGroupSlotMembership.objects.filter_by_organization(organization.id).filter(
        slot_fk=slot, calendar_fk=calendar
    )
    assert set(rows.values_list("id", flat=True)) == {inline.id, projected.id}
    assert {row.source_pool_fk_id for row in rows} == {None, pool.id}


@pytest.mark.django_db(transaction=True)
def test_two_projected_rows_from_different_pools_are_accepted(
    organization, slot, calendar, pool, other_pool
):
    _membership(organization, slot, calendar, source_pool=pool)
    _membership(organization, slot, calendar, source_pool=other_pool)

    assert (
        CalendarGroupSlotMembership.objects.filter_by_organization(organization.id)
        .filter(slot_fk=slot, calendar_fk=calendar)
        .count()
        == 2
    )


# ---------------------------------------------------------------------------
# The indexes the swap actually installed
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_constraint_swap_left_the_expected_partial_indexes(db):
    """Reads ``pg_indexes`` directly: the whole point of this phase is what
    Postgres enforces, not what the Django model declares.

    ``pg_indexes`` is catalog metadata, not tenant data, so reading it with a
    cursor does not touch the multi-tenancy contract.
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s ORDER BY indexname",
            ["calendar_integration_calendargroupslotmembership"],
        )
        indexes = dict(cursor.fetchall())

    assert "calendargroupslotmembership_unique_slot_calendar" not in indexes

    inline_def = indexes["calendargroupslotmembership_uniq_inline"]
    assert "UNIQUE INDEX" in inline_def
    assert "(slot_fk_id, calendar_fk_id)" in inline_def
    assert "source_pool_fk_id IS NULL" in inline_def

    projected_def = indexes["calendargroupslotmembership_uniq_projected"]
    assert "UNIQUE INDEX" in projected_def
    assert "(slot_fk_id, calendar_fk_id, source_pool_fk_id)" in projected_def
    assert "source_pool_fk_id IS NOT NULL" in projected_def
