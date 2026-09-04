from django.db import IntegrityError

import pytest
from model_bakery import baker

from calendar_integration.models import Calendar, CalendarPool, CalendarPoolMembership


@pytest.mark.django_db
def test_calendar_pool_str():
    org = baker.make("organizations.Organization")
    pool = CalendarPool.objects.create(organization=org, name="Nurses")

    assert str(pool) == "Nurses"


@pytest.mark.django_db
def test_calendar_pool_unique_name_per_org():
    org = baker.make("organizations.Organization")
    CalendarPool.objects.create(organization=org, name="Nurses")

    with pytest.raises(IntegrityError):
        CalendarPool.objects.create(organization=org, name="Nurses")


@pytest.mark.django_db
def test_calendar_pool_same_name_different_org_allowed():
    org1 = baker.make("organizations.Organization")
    org2 = baker.make("organizations.Organization")

    CalendarPool.objects.create(organization=org1, name="Nurses")
    CalendarPool.objects.create(organization=org2, name="Nurses")  # should not raise

    assert CalendarPool.objects.filter_by_organization(org1.id).count() == 1
    assert CalendarPool.objects.filter_by_organization(org2.id).count() == 1


@pytest.mark.django_db
def test_calendar_pool_membership_unique():
    org = baker.make("organizations.Organization")
    pool = CalendarPool.objects.create(organization=org, name="Nurses")
    calendar = baker.make(Calendar, organization=org, external_id=baker.seq("cal"))

    CalendarPoolMembership.objects.create(organization=org, pool=pool, calendar=calendar)

    with pytest.raises(IntegrityError):
        CalendarPoolMembership.objects.create(organization=org, pool=pool, calendar=calendar)


@pytest.mark.django_db
def test_calendar_pool_membership_str():
    org = baker.make("organizations.Organization")
    pool = CalendarPool.objects.create(organization=org, name="Nurses")
    calendar = baker.make(Calendar, organization=org, external_id=baker.seq("cal"))

    membership = CalendarPoolMembership.objects.create(
        organization=org, pool=pool, calendar=calendar
    )

    assert str(membership) == f"{calendar.id} in pool {pool.id}"


@pytest.mark.django_db
def test_calendar_pool_calendars_m2m():
    org = baker.make("organizations.Organization")
    pool = CalendarPool.objects.create(organization=org, name="Nurses")
    cal1 = baker.make(Calendar, organization=org, external_id="cal-m2m-1")
    cal2 = baker.make(Calendar, organization=org, external_id="cal-m2m-2")

    CalendarPoolMembership.objects.create(organization=org, pool=pool, calendar=cal1)
    CalendarPoolMembership.objects.create(organization=org, pool=pool, calendar=cal2)

    assert set(pool.calendars.values_list("id", flat=True)) == {cal1.id, cal2.id}
    assert set(cal1.pools.values_list("id", flat=True)) == {pool.id}


@pytest.mark.django_db
def test_calendar_pool_membership_cross_organization_safe_relation():
    """A membership row can only ever join a pool and a calendar sharing its
    organization: the ``pool`` / ``calendar`` safe relations carry the
    organization in their ``ON`` clause, so a membership row pointing (via the
    concrete ``_fk`` column) at a pool from another organization is simply
    invisible through the safe relation rather than silently joined.
    """
    org1 = baker.make("organizations.Organization")
    org2 = baker.make("organizations.Organization")
    pool_org1 = CalendarPool.objects.create(organization=org1, name="Nurses")
    calendar_org2 = baker.make(Calendar, organization=org2, external_id=baker.seq("cal"))

    # Constructed by setting the concrete FK columns directly (bypassing the
    # safe-relation assignment, which would copy the target's organization).
    membership = CalendarPoolMembership.objects.create(
        organization=org1,
        pool_fk_id=pool_org1.id,
        calendar_fk_id=calendar_org2.id,
    )

    # The safe relation for a cross-org row does not resolve to the calendar.
    assert (
        CalendarPoolMembership.objects.filter_by_organization(org1.id)
        .filter(id=membership.id, calendar_fk_id=calendar_org2.id)
        .exists()
    )
    assert (
        not CalendarPoolMembership.objects.filter_by_organization(org1.id)
        .filter(id=membership.id, calendar=calendar_org2)
        .exists()
    )


@pytest.mark.django_db
def test_calendar_pool_other_org_not_visible():
    org1 = baker.make("organizations.Organization")
    org2 = baker.make("organizations.Organization")
    CalendarPool.objects.create(organization=org1, name="Nurses")

    assert CalendarPool.objects.filter_by_organization(org2.id).count() == 0
