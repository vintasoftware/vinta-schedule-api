"""CalendarOwnership membership cutover tests at the app layer.

Covers the membership-scoped read/write path that replaces the bare ``user`` FK:

- ownership creation through ``CalendarService`` sets ``membership_user_id``;
- membership-based reads (default-calendar resolution, owner checks) resolve
  through the denormalized ``membership_user_id`` / the ``membership``
  ForeignObject join;
- the ``Calendar.memberships`` M2M returns the owning memberships;
- orphan ownerships (``(user, org)`` with no membership) are NOT returned by any
  membership-based read or the M2M — the intended end state.

The ``user`` column still exists; these tests assert membership behaviour without
depending on the (now unused-by-app-code) ``user`` reverse accessors.
"""

from __future__ import annotations

import pytest
from model_bakery import baker

from calendar_integration.factories import create_calendar_ownership
from calendar_integration.models import Calendar, CalendarOwnership
from calendar_integration.serializers import (
    CalendarOwnershipSerializer,
    OwnershipMembershipSerializer,
)
from calendar_integration.services.calendar_service import CalendarService
from organizations.models import Organization, OrganizationMembership, OrganizationRole


@pytest.fixture
def organization(db) -> Organization:
    return baker.make(Organization)


@pytest.fixture
def member_user(organization):
    user = baker.make("users.User")
    OrganizationMembership.objects.create(user=user, organization=organization)
    return user


@pytest.fixture
def calendar(organization) -> Calendar:
    return baker.make(Calendar, organization=organization)


# ---------------------------------------------------------------------------
# Write path — ownership create sets membership
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_virtual_calendar_sets_membership(organization, member_user):
    """Creating a calendar as a member sets ``membership_user_id`` on the ownership row."""
    service = CalendarService()
    service.initialize_without_provider(user_or_token=member_user, organization=organization)

    cal = service.create_virtual_calendar(name="Mine", description="d")

    ownership = CalendarOwnership.objects.filter_by_organization(organization.id).get(
        calendar_fk_id=cal.id
    )
    assert ownership.membership_user_id == member_user.id
    # The ForeignObject join resolves to the member's active membership.
    resolved = (
        CalendarOwnership.objects.filter_by_organization(organization.id)
        .select_related("membership")
        .get(pk=ownership.pk)
    )
    assert resolved.membership is not None
    assert resolved.membership.user_id == member_user.id
    assert resolved.membership.organization_id == organization.id


# ---------------------------------------------------------------------------
# Read path — default-calendar + owner checks resolve via membership
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_default_calendar_resolves_via_membership(organization, member_user, calendar):
    """``get_default_calendar_for_user`` resolves through ``membership_user_id``."""
    create_calendar_ownership(calendar=calendar, user=member_user, is_default=True)

    service = CalendarService()
    service.initialize_without_provider(user_or_token=member_user, organization=organization)

    resolved = service.get_default_calendar_for_user(member_user)
    assert resolved == calendar


@pytest.mark.django_db
def test_owner_check_resolves_via_membership(organization, member_user, calendar):
    """An ownership lookup keyed on ``membership_user_id`` finds the membership-backed owner."""
    create_calendar_ownership(calendar=calendar, user=member_user)

    assert (
        CalendarOwnership.objects.filter_by_organization(organization.id)
        .filter(membership_user_id=member_user.id, calendar=calendar)
        .exists()
    )


# ---------------------------------------------------------------------------
# Calendar.memberships M2M
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_calendar_memberships_m2m_returns_owning_membership(organization, member_user, calendar):
    """``Calendar.memberships`` returns the owning OrganizationMembership."""
    create_calendar_ownership(calendar=calendar, user=member_user)

    membership = OrganizationMembership.objects.get(user=member_user, organization=organization)
    cal = Calendar.objects.filter_by_organization(organization.id).get(pk=calendar.pk)
    assert list(cal.memberships.all()) == [membership]


# ---------------------------------------------------------------------------
# Orphan ownership behaviour (intended end state — NOT a regression)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_orphan_ownership_excluded_from_membership_reads(organization, calendar):
    """An orphan ownership (no membership) is invisible to membership-based reads."""
    orphan_user = baker.make("users.User")  # NOT a member of organization
    ownership = create_calendar_ownership(
        calendar=calendar, user=orphan_user, with_membership=False
    )

    # Row exists but carries no membership (orphan).
    assert ownership.membership_user_id is None

    # Membership-based filter excludes the orphan.
    assert not (
        CalendarOwnership.objects.filter_by_organization(organization.id)
        .filter(membership_user_id=orphan_user.id)
        .exists()
    )


@pytest.mark.django_db
def test_orphan_ownership_excluded_from_memberships_m2m(organization, calendar):
    """An orphan ownership does not surface through ``Calendar.memberships``."""
    orphan_user = baker.make("users.User")
    create_calendar_ownership(calendar=calendar, user=orphan_user, with_membership=False)

    cal = Calendar.objects.filter_by_organization(organization.id).get(pk=calendar.pk)
    assert list(cal.memberships.all()) == []


# ---------------------------------------------------------------------------
# REST serializer — membership identity shape { user_id, organization_id, role }
# ---------------------------------------------------------------------------


def _resolved_ownership(organization, ownership):
    return (
        CalendarOwnership.objects.filter_by_organization(organization.id)
        .select_related("membership")
        .get(pk=ownership.pk)
    )


@pytest.mark.django_db
def test_ownership_serializer_membership_field_shape(organization, calendar):
    """The REST ownership serializer exposes membership identity { user_id, organization_id, role }.

    The membership representation is supplied by ``OwnershipMembershipSerializer``,
    nested on ``CalendarOwnershipSerializer`` in place of the old bare ``user``.
    """
    user = baker.make("users.User")
    OrganizationMembership.objects.create(
        user=user, organization=organization, role=OrganizationRole.ADMIN
    )
    ownership = _resolved_ownership(
        organization, create_calendar_ownership(calendar=calendar, user=user)
    )

    # The serializer no longer exposes a bare user; membership replaces it.
    assert "user" not in CalendarOwnershipSerializer.Meta.fields
    assert "membership" in CalendarOwnershipSerializer.Meta.fields

    data = OwnershipMembershipSerializer(ownership.membership).data
    assert data == {
        "user_id": user.id,
        "organization_id": organization.id,
        "role": OrganizationRole.ADMIN,
    }


@pytest.mark.django_db
def test_ownership_serializer_orphan_membership_is_null(organization, calendar):
    """An orphan ownership has a null ``membership`` (no nested identity to expose)."""
    orphan_user = baker.make("users.User")
    ownership = _resolved_ownership(
        organization,
        create_calendar_ownership(calendar=calendar, user=orphan_user, with_membership=False),
    )
    assert ownership.membership is None


@pytest.mark.django_db
def test_default_calendar_orphan_returns_none(organization, calendar):
    """Default-calendar resolution returns None for an orphan ownership."""
    orphan_user = baker.make("users.User")
    create_calendar_ownership(
        calendar=calendar, user=orphan_user, is_default=True, with_membership=False
    )

    service = CalendarService()
    service.initialize_without_provider(user_or_token=orphan_user, organization=organization)

    assert service.get_default_calendar_for_user(orphan_user) is None
