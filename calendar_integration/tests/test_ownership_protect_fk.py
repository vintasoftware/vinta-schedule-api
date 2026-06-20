"""Phase 2b cutover (DB half) — CalendarOwnership membership PROTECT FK + unique.

After Phase 2b the legacy ``user`` column is gone and ownership integrity is
enforced at the DB level:

- a raw-SQL composite FK ``(membership_user_id, organization_id) ->
  OrganizationMembership(user_id, organization_id) ON DELETE RESTRICT`` enforces
  PROTECT delete semantics on the ForeignObject relation;
- a partial unique constraint ``(calendar_fk, membership_user_id) WHERE
  membership_user_id IS NOT NULL`` prevents two ownerships for the same member on
  one calendar, while still permitting multiple NULL (orphan) rows.

These tests exercise the DB constraints directly (raising ``IntegrityError``),
so they run with ``transaction=True`` and assert inside ``transaction.atomic``
blocks where a failed statement would otherwise poison the surrounding
transaction.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction

import pytest
from model_bakery import baker

from calendar_integration.factories import create_calendar_ownership
from calendar_integration.models import Calendar, CalendarOwnership
from organizations.models import Organization, OrganizationMembership
from users.models import User


@pytest.fixture
def organization(db) -> Organization:
    return baker.make(Organization)


@pytest.fixture
def member_user(organization) -> User:
    user = baker.make("users.User")
    OrganizationMembership.objects.create(user=user, organization=organization)
    return user


@pytest.fixture
def calendar(organization) -> Calendar:
    return baker.make(Calendar, organization=organization)


# ---------------------------------------------------------------------------
# PROTECT — deleting a referenced membership / user is blocked
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_delete_membership_with_live_ownership_is_blocked(organization, member_user, calendar):
    """Deleting an OrganizationMembership referenced by a live ownership raises IntegrityError."""
    create_calendar_ownership(calendar=calendar, user=member_user)
    membership = OrganizationMembership.objects.get(user=member_user, organization=organization)

    with pytest.raises(IntegrityError), transaction.atomic():
        membership.delete()


@pytest.mark.django_db(transaction=True)
def test_delete_user_with_live_ownership_is_blocked(organization, member_user, calendar):
    """Deleting the User cascades to its membership, which the PROTECT FK blocks.

    Documented behaviour change introduced in Phase 2b: a User that owns a calendar
    through their membership can no longer be deleted while the ownership is live —
    the membership-cascade hits the ON DELETE RESTRICT FK.
    """
    create_calendar_ownership(calendar=calendar, user=member_user)

    with pytest.raises(IntegrityError), transaction.atomic():
        member_user.delete()


@pytest.mark.django_db(transaction=True)
def test_delete_membership_allowed_after_ownership_removed(organization, member_user, calendar):
    """Once the ownership is gone, the membership can be deleted normally."""
    ownership = create_calendar_ownership(calendar=calendar, user=member_user)
    membership = OrganizationMembership.objects.get(user=member_user, organization=organization)

    ownership.delete()
    membership.delete()  # no error

    assert not OrganizationMembership.objects.filter(pk=membership.pk).exists()


# ---------------------------------------------------------------------------
# FK enforcement — non-NULL membership_user_id must reference a membership
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_ownership_with_nonexistent_membership_raises(organization, calendar):
    """A non-NULL membership_user_id without a matching membership violates the FK."""
    non_member = baker.make("users.User")  # NOT a member of organization

    with pytest.raises(IntegrityError), transaction.atomic():
        CalendarOwnership.objects.create(
            organization=organization,
            calendar=calendar,
            membership_user_id=non_member.id,
        )


@pytest.mark.django_db(transaction=True)
def test_ownership_update_to_nonexistent_membership_raises(organization, member_user, calendar):
    """Updating membership_user_id to a non-member value violates the FK."""
    ownership = create_calendar_ownership(calendar=calendar, user=member_user)
    non_member = baker.make("users.User")

    with pytest.raises(IntegrityError), transaction.atomic():
        CalendarOwnership.original_manager.filter(pk=ownership.pk).update(
            membership_user_id=non_member.id
        )


@pytest.mark.django_db(transaction=True)
def test_orphan_ownership_null_membership_allowed(organization, calendar):
    """membership_user_id=NULL (orphan) is allowed — the FK does not constrain NULLs."""
    ownership = CalendarOwnership.objects.create(
        organization=organization,
        calendar=calendar,
        membership_user_id=None,
    )
    assert ownership.membership_user_id is None


# ---------------------------------------------------------------------------
# Partial unique constraint — one ownership per (calendar, member)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_duplicate_member_ownership_on_calendar_violates_unique(
    organization, member_user, calendar
):
    """Two ownerships for the same (calendar, membership_user_id) violate the partial unique."""
    create_calendar_ownership(calendar=calendar, user=member_user)

    with pytest.raises(IntegrityError), transaction.atomic():
        CalendarOwnership.objects.create(
            organization=organization,
            calendar=calendar,
            membership_user_id=member_user.id,
        )


@pytest.mark.django_db(transaction=True)
def test_multiple_orphan_ownerships_on_calendar_allowed(organization, calendar):
    """Two NULL (orphan) ownerships for the same calendar are allowed (partial unique)."""
    first = CalendarOwnership.objects.create(
        organization=organization,
        calendar=calendar,
        membership_user_id=None,
    )
    second = CalendarOwnership.objects.create(
        organization=organization,
        calendar=calendar,
        membership_user_id=None,
    )
    assert first.pk != second.pk
    assert (
        CalendarOwnership.objects.filter_by_organization(organization.id)
        .filter(calendar=calendar, membership_user_id__isnull=True)
        .count()
        == 2
    )
