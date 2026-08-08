"""Unit tests for ``CalendarGroupSlotQuotaRule`` (Phase 3a of
CALENDAR_GROUP_SCOPED_AVAILABILITY).

Covers the model's own constraints -- unique per (calendar, slot, period) and
a positive cap -- plus cascade behavior when the slot (or the group it
belongs to) is deleted. Membership-removal cleanup (a calendar taken out of a
slot's roster while the slot survives) is covered separately in
``calendar_integration/tests/services/test_calendar_group_service_quota_reconcile.py``,
since that's service-layer behavior (``CalendarGroupService._reconcile_slot``),
not a schema-level cascade.
"""

from __future__ import annotations

from django.db import IntegrityError

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarProvider, CalendarType, QuotaPeriod
from calendar_integration.factories import create_group_slot_quota_rule
from calendar_integration.models import (
    Calendar,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarGroupSlotQuotaRule,
)
from organizations.models import Organization


@pytest.fixture
def organization(db):
    return baker.make("organizations.Organization")


@pytest.fixture
def calendar(organization) -> Calendar:
    return Calendar.objects.create(
        organization=organization,
        name="Dr. Reyes",
        external_id="quota-model-cal",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
    )


@pytest.fixture
def group_slot(organization, calendar: Calendar) -> CalendarGroupSlot:
    group = CalendarGroup.objects.create(organization=organization, name="Surgery")
    slot = CalendarGroupSlot.objects.create(organization=organization, group=group, name="Lead")
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=calendar
    )
    return slot


@pytest.mark.django_db
def test_create_quota_rule(organization, calendar: Calendar, group_slot: CalendarGroupSlot):
    rule = create_group_slot_quota_rule(
        organization=organization,
        group_slot=group_slot,
        calendar=calendar,
        period=QuotaPeriod.WEEK,
        cap=3,
    )

    assert rule.group_slot_fk_id == group_slot.id
    assert rule.calendar_fk_id == calendar.id
    assert rule.period == QuotaPeriod.WEEK
    assert rule.cap == 3
    assert str(rule.calendar_fk_id) in str(rule)


@pytest.mark.django_db
def test_unique_per_calendar_slot_period(
    organization, calendar: Calendar, group_slot: CalendarGroupSlot
):
    create_group_slot_quota_rule(
        organization=organization, group_slot=group_slot, calendar=calendar, period=QuotaPeriod.WEEK
    )

    with pytest.raises(IntegrityError):
        create_group_slot_quota_rule(
            organization=organization,
            group_slot=group_slot,
            calendar=calendar,
            period=QuotaPeriod.WEEK,
        )


@pytest.mark.django_db
def test_multiple_periods_for_same_calendar_and_slot_allowed(
    organization, calendar: Calendar, group_slot: CalendarGroupSlot
):
    """Daily AND weekly caps on the same (calendar, slot) must coexist --
    that's the whole point of "at most 1 a day AND 3 a week"."""
    daily = create_group_slot_quota_rule(
        organization=organization,
        group_slot=group_slot,
        calendar=calendar,
        period=QuotaPeriod.DAY,
        cap=1,
    )
    weekly = create_group_slot_quota_rule(
        organization=organization,
        group_slot=group_slot,
        calendar=calendar,
        period=QuotaPeriod.WEEK,
        cap=3,
    )

    assert set(
        CalendarGroupSlotQuotaRule.objects.filter_by_organization(organization.id)
        .filter(group_slot_fk=group_slot, calendar_fk=calendar)
        .values_list("id", flat=True)
    ) == {daily.id, weekly.id}


@pytest.mark.django_db
def test_cap_must_be_positive(organization, calendar: Calendar, group_slot: CalendarGroupSlot):
    with pytest.raises(IntegrityError):
        create_group_slot_quota_rule(
            organization=organization,
            group_slot=group_slot,
            calendar=calendar,
            period=QuotaPeriod.WEEK,
            cap=0,
        )


@pytest.mark.django_db
def test_cascade_delete_on_slot_deletion(
    organization, calendar: Calendar, group_slot: CalendarGroupSlot
):
    rule = create_group_slot_quota_rule(
        organization=organization, group_slot=group_slot, calendar=calendar
    )

    group_slot.delete()

    assert (
        not CalendarGroupSlotQuotaRule.objects.filter_by_organization(organization.id)
        .filter(id=rule.id)
        .exists()
    )


@pytest.mark.django_db
def test_cascade_delete_on_group_deletion(
    organization, calendar: Calendar, group_slot: CalendarGroupSlot
):
    rule = create_group_slot_quota_rule(
        organization=organization, group_slot=group_slot, calendar=calendar
    )

    group_slot.group.delete()

    assert (
        not CalendarGroupSlotQuotaRule.objects.filter_by_organization(organization.id)
        .filter(id=rule.id)
        .exists()
    )


@pytest.mark.django_db
def test_cascade_delete_on_calendar_deletion(
    organization, calendar: Calendar, group_slot: CalendarGroupSlot
):
    rule = create_group_slot_quota_rule(
        organization=organization, group_slot=group_slot, calendar=calendar
    )

    calendar.delete()

    assert (
        not CalendarGroupSlotQuotaRule.objects.filter_by_organization(organization.id)
        .filter(id=rule.id)
        .exists()
    )


@pytest.mark.django_db
def test_same_calendar_slot_period_different_org_allowed(calendar: Calendar):
    """Uniqueness is scoped by the tenant-safe FKs, not a bare unique -- two
    different orgs must be able to define identical rules."""
    org1: Organization = baker.make("organizations.Organization")
    org2: Organization = baker.make("organizations.Organization")

    cal1 = Calendar.objects.create(
        organization=org1,
        name="Cal 1",
        external_id="quota-cross-org-1",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
    )
    cal2 = Calendar.objects.create(
        organization=org2,
        name="Cal 2",
        external_id="quota-cross-org-2",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
    )

    group1 = CalendarGroup.objects.create(organization=org1, name="Clinic")
    slot1 = CalendarGroupSlot.objects.create(organization=org1, group=group1, name="Lead")
    CalendarGroupSlotMembership.objects.create(organization=org1, slot=slot1, calendar=cal1)

    group2 = CalendarGroup.objects.create(organization=org2, name="Clinic")
    slot2 = CalendarGroupSlot.objects.create(organization=org2, group=group2, name="Lead")
    CalendarGroupSlotMembership.objects.create(organization=org2, slot=slot2, calendar=cal2)

    create_group_slot_quota_rule(
        organization=org1, group_slot=slot1, calendar=cal1, period=QuotaPeriod.WEEK, cap=3
    )
    # Should not raise -- different organization entirely.
    create_group_slot_quota_rule(
        organization=org2, group_slot=slot2, calendar=cal2, period=QuotaPeriod.WEEK, cap=3
    )
