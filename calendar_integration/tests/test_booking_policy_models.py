"""Unit tests for the ``BookingPolicy`` model.

Covers the single-target rule (check constraint), the per-target partial unique
indexes, negative-value rejection, the factory, and the manager's per-target
lookups.
"""

from django.db import IntegrityError, transaction

import pytest
from model_bakery import baker

from calendar_integration.factories import create_booking_policy
from calendar_integration.models import BookingPolicy, Calendar, CalendarGroup


def _make_calendar(org, **extra) -> Calendar:
    return baker.make(Calendar, organization=org, name="Cal", **extra)


def _make_membership(org):
    """Create an active membership and return its denormalized user id."""
    from organizations.models import OrganizationMembership

    user = baker.make("users.User")
    OrganizationMembership.objects.create(user=user, organization=org)
    return user.id


@pytest.mark.django_db
def test_factory_builds_valid_calendar_policy():
    org = baker.make("organizations.Organization")
    calendar = _make_calendar(org)

    policy = create_booking_policy(calendar=calendar, lead_time_seconds=3600)

    assert policy.pk is not None
    assert policy.calendar_fk_id == calendar.id
    assert policy.membership_user_id is None
    assert policy.calendar_group_fk_id is None
    assert policy.is_organization_default is False
    assert policy.lead_time_seconds == 3600


@pytest.mark.django_db
def test_factory_builds_valid_membership_policy():
    org = baker.make("organizations.Organization")
    membership_user_id = _make_membership(org)

    policy = create_booking_policy(membership_user_id=membership_user_id, organization=org)

    assert policy.membership_user_id == membership_user_id
    assert policy.calendar_fk_id is None


@pytest.mark.django_db
def test_factory_builds_valid_group_policy():
    org = baker.make("organizations.Organization")
    group = CalendarGroup.objects.create(organization=org, name="Clinic")

    policy = create_booking_policy(calendar_group=group)

    assert policy.calendar_group_fk_id == group.id


@pytest.mark.django_db
def test_factory_builds_valid_org_default_policy():
    org = baker.make("organizations.Organization")

    policy = create_booking_policy(is_organization_default=True, organization=org)

    assert policy.is_organization_default is True
    assert policy.calendar_fk_id is None


@pytest.mark.django_db
def test_factory_rejects_zero_targets():
    org = baker.make("organizations.Organization")

    with pytest.raises(ValueError, match="exactly one target"):
        create_booking_policy(organization=org)


@pytest.mark.django_db
def test_factory_rejects_multiple_targets():
    org = baker.make("organizations.Organization")
    calendar = _make_calendar(org)

    with pytest.raises(ValueError, match="exactly one target"):
        create_booking_policy(calendar=calendar, is_organization_default=True)


@pytest.mark.django_db
def test_check_constraint_rejects_zero_target():
    org = baker.make("organizations.Organization")

    with pytest.raises(IntegrityError):
        BookingPolicy.objects.create(organization=org)


@pytest.mark.django_db
def test_check_constraint_rejects_multi_target_calendar_and_group():
    org = baker.make("organizations.Organization")
    calendar = _make_calendar(org)
    group = CalendarGroup.objects.create(organization=org, name="Clinic")

    with pytest.raises(IntegrityError):
        BookingPolicy.objects.create(organization=org, calendar=calendar, calendar_group=group)


@pytest.mark.django_db
def test_check_constraint_rejects_target_plus_org_default():
    org = baker.make("organizations.Organization")
    calendar = _make_calendar(org)

    with pytest.raises(IntegrityError):
        BookingPolicy.objects.create(
            organization=org, calendar=calendar, is_organization_default=True
        )


@pytest.mark.django_db
def test_uniq_calendar_rejects_duplicate():
    org = baker.make("organizations.Organization")
    calendar = _make_calendar(org)
    create_booking_policy(calendar=calendar)

    with pytest.raises(IntegrityError):
        create_booking_policy(calendar=calendar)


@pytest.mark.django_db
def test_uniq_membership_rejects_duplicate():
    org = baker.make("organizations.Organization")
    membership_user_id = _make_membership(org)
    create_booking_policy(membership_user_id=membership_user_id, organization=org)

    with pytest.raises(IntegrityError):
        create_booking_policy(membership_user_id=membership_user_id, organization=org)


@pytest.mark.django_db
def test_uniq_group_rejects_duplicate():
    org = baker.make("organizations.Organization")
    group = CalendarGroup.objects.create(organization=org, name="Clinic")
    create_booking_policy(calendar_group=group)

    with pytest.raises(IntegrityError):
        create_booking_policy(calendar_group=group)


@pytest.mark.django_db
def test_uniq_org_default_rejects_duplicate():
    org = baker.make("organizations.Organization")
    create_booking_policy(is_organization_default=True, organization=org)

    with pytest.raises(IntegrityError):
        create_booking_policy(is_organization_default=True, organization=org)


@pytest.mark.django_db
def test_org_default_allowed_in_two_different_orgs():
    org1 = baker.make("organizations.Organization")
    org2 = baker.make("organizations.Organization")

    create_booking_policy(is_organization_default=True, organization=org1)
    create_booking_policy(is_organization_default=True, organization=org2)  # no raise

    assert BookingPolicy.objects.filter_by_organization(org1.id).count() == 1
    assert BookingPolicy.objects.filter_by_organization(org2.id).count() == 1


@pytest.mark.django_db
def test_negative_lead_time_rejected():
    org = baker.make("organizations.Organization")
    calendar = _make_calendar(org)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            BookingPolicy.objects.create(organization=org, calendar=calendar, lead_time_seconds=-1)


@pytest.mark.django_db
def test_negative_buffer_rejected():
    org = baker.make("organizations.Organization")
    calendar = _make_calendar(org)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            BookingPolicy.objects.create(
                organization=org, calendar=calendar, buffer_after_seconds=-5
            )


@pytest.mark.django_db
def test_manager_for_target_returns_matching_policy():
    org = baker.make("organizations.Organization")
    calendar = _make_calendar(org)
    policy = create_booking_policy(calendar=calendar)

    found = BookingPolicy.objects.filter_by_organization(org.id).for_calendar(calendar.id)
    assert found.get().pk == policy.pk


@pytest.mark.django_db
def test_manager_org_default_lookup():
    org = baker.make("organizations.Organization")
    policy = create_booking_policy(is_organization_default=True, organization=org)

    found = BookingPolicy.objects.filter_by_organization(org.id).org_default().get()
    assert found.pk == policy.pk


@pytest.mark.django_db
def test_manager_for_target_org_scoped_returns_row_and_none():
    """``BookingPolicyManager.for_target(org_id, ...)`` is org-scoped and returns the row or None."""
    org = baker.make("organizations.Organization")
    calendar = _make_calendar(org)
    policy = create_booking_policy(calendar=calendar)

    found = BookingPolicy.objects.for_target(org.id, calendar_id=calendar.id)
    assert found is not None
    assert found.pk == policy.pk

    other_calendar = _make_calendar(org, external_id="other-cal")
    assert BookingPolicy.objects.for_target(org.id, calendar_id=other_calendar.id) is None


@pytest.mark.django_db
def test_manager_org_default_org_scoped_returns_row_and_none():
    """``BookingPolicyManager.org_default(org_id)`` is org-scoped and returns the row or None."""
    org = baker.make("organizations.Organization")
    policy = create_booking_policy(is_organization_default=True, organization=org)

    found = BookingPolicy.objects.org_default(org.id)
    assert found is not None
    assert found.pk == policy.pk

    other_org = baker.make("organizations.Organization")
    assert BookingPolicy.objects.org_default(other_org.id) is None


@pytest.mark.django_db(transaction=True)
def test_membership_composite_fk_rejects_nonexistent_membership():
    """A membership policy pointing at a non-existent membership raises at commit.

    The composite FK is ``DEFERRABLE INITIALLY DEFERRED``, so the violation only
    surfaces when the transaction COMMITs — hence ``transaction=True`` and the
    explicit ``transaction.atomic()`` block.
    """
    org = baker.make("organizations.Organization")

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            BookingPolicy.objects.create(
                organization=org,
                membership_user_id=999_999_999,
            )


@pytest.mark.django_db(transaction=True)
def test_membership_delete_blocked_while_policy_live():
    """Deleting a membership referenced by a live policy is blocked at commit (deferred NO ACTION)."""
    from organizations.models import OrganizationMembership

    org = baker.make("organizations.Organization")
    user = baker.make("users.User")
    membership = OrganizationMembership.objects.create(user=user, organization=org)
    create_booking_policy(membership_user_id=user.id, organization=org)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            membership.delete()


@pytest.mark.django_db
def test_str_describes_target():
    org = baker.make("organizations.Organization")
    calendar = _make_calendar(org)
    policy = create_booking_policy(calendar=calendar)

    assert f"calendar {calendar.id}" in str(policy)
