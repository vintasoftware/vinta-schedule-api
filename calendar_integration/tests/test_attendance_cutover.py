"""EventAttendance membership cutover tests at the app layer.

Covers the membership-scoped read/write path that replaces the bare ``user`` FK on
``EventAttendance``:

- attendance creation through ``CalendarEventService`` sets ``membership_user_id``
  for members and leaves it NULL for non-member (orphan) attendees;
- attendee identity serialization resolves name/email via the denormalized
  ``membership_user_id`` (so it survives the ``user`` column drop);
- the ``CalendarEvent.attendee_memberships`` M2M returns the attendee memberships
  and excludes orphan attendances;
- the GraphQL + REST attendance API expose the membership identity
  ``{ user_id, organization_id, role }`` (None for orphans).

The ``user`` column still exists; these tests assert membership behaviour without
depending on the (now unused-by-app-code) ``user`` reverse accessors.
"""

from __future__ import annotations

import datetime

import pytest
from model_bakery import baker

from calendar_integration.graphql import EventAttendanceGraphQLType
from calendar_integration.models import Calendar, CalendarEvent, EventAttendance
from calendar_integration.serializers import (
    EventAttendanceSerializer,
    OwnershipMembershipSerializer,
)
from calendar_integration.services.calendar_service_utils import resolve_member_user_ids
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


@pytest.fixture
def event(organization, calendar) -> CalendarEvent:
    return baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        title="Test Event",
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 10, 0, tzinfo=datetime.UTC),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
    )


def _make_attendance(event, organization, *, membership_user_id):
    return EventAttendance.objects.create(
        event=event,
        organization=organization,
        membership_user_id=membership_user_id,
        status="pending",
    )


# ---------------------------------------------------------------------------
# Write path — membership resolution guard (shared by every attendance create)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_resolve_member_user_ids_returns_only_members(organization, member_user):
    """The bulk attendance-write guard resolves only the member user_ids.

    Both the event-service bulk_create and the sync attendance create set
    ``membership_user_id`` from this guard: members get their id, non-members
    (orphans) resolve to nothing.
    """
    non_member = baker.make("users.User")  # NOT a member of organization

    resolved = resolve_member_user_ids([member_user.id, non_member.id], organization.id)

    assert resolved == {member_user.id}


@pytest.mark.django_db
def test_resolve_member_user_ids_scoped_to_organization(member_user):
    """A membership in another organization does not satisfy the guard."""
    other_org = baker.make(Organization)

    assert resolve_member_user_ids([member_user.id], other_org.id) == set()


# ---------------------------------------------------------------------------
# attendee_memberships M2M
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_attendee_memberships_m2m_returns_member_attendees(organization, event, member_user):
    """``CalendarEvent.attendee_memberships`` returns the attendee membership."""
    _make_attendance(event, organization, membership_user_id=member_user.id)

    membership = OrganizationMembership.objects.get(user=member_user, organization=organization)
    resolved = CalendarEvent.objects.filter_by_organization(organization.id).get(pk=event.pk)
    assert list(resolved.attendee_memberships.all()) == [membership]


@pytest.mark.django_db
def test_orphan_attendance_excluded_from_attendee_memberships_m2m(organization, event):
    """An orphan attendance does not surface through ``attendee_memberships``."""
    _make_attendance(event, organization, membership_user_id=None)

    resolved = CalendarEvent.objects.filter_by_organization(organization.id).get(pk=event.pk)
    assert list(resolved.attendee_memberships.all()) == []


# ---------------------------------------------------------------------------
# REST serializer — membership identity shape { user_id, organization_id, role }
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_attendance_serializer_membership_field_shape(organization, event):
    """The REST attendance serializer exposes membership identity, not a bare user."""
    user = baker.make("users.User")
    OrganizationMembership.objects.create(
        user=user, organization=organization, role=OrganizationRole.ADMIN
    )
    _make_attendance(event, organization, membership_user_id=user.id)

    attendance = (
        EventAttendance.objects.filter_by_organization(organization.id)
        .select_related("membership")
        .get(event_fk_id=event.id, membership_user_id=user.id)
    )

    # The serializer no longer exposes a bare user; membership replaces it.
    assert "user" not in EventAttendanceSerializer.Meta.fields
    assert "membership" in EventAttendanceSerializer.Meta.fields

    data = OwnershipMembershipSerializer(attendance.membership).data
    assert data == {
        "user_id": user.id,
        "organization_id": organization.id,
        "role": OrganizationRole.ADMIN,
    }


@pytest.mark.django_db
def test_attendance_serializer_orphan_membership_is_null(organization, event):
    """An orphan attendance has a null ``membership`` (no nested identity to expose)."""
    created = _make_attendance(event, organization, membership_user_id=None)

    attendance = (
        EventAttendance.objects.filter_by_organization(organization.id)
        .select_related("membership")
        .get(pk=created.pk)
    )
    assert attendance.membership is None
    assert attendance.membership_user_id is None


# ---------------------------------------------------------------------------
# GraphQL type — membership identity { user_id, organization_id, role }
# ---------------------------------------------------------------------------


def test_attendance_graphql_exposes_membership_field_not_user():
    """The GraphQL attendance type exposes ``membership`` instead of a bare ``user``."""
    field_names = {
        f.name
        for f in EventAttendanceGraphQLType.__strawberry_definition__.fields  # type: ignore[attr-defined]
    }
    assert "membership" in field_names
    assert "user" not in field_names


@pytest.mark.django_db
def test_attendance_graphql_membership_resolver(organization, event):
    """The GraphQL attendance type's membership resolver returns the member identity."""
    user = baker.make("users.User")
    OrganizationMembership.objects.create(
        user=user, organization=organization, role=OrganizationRole.MEMBER
    )
    _make_attendance(event, organization, membership_user_id=user.id)

    attendance = (
        EventAttendance.objects.filter_by_organization(organization.id)
        .select_related("membership")
        .get(event_fk_id=event.id, membership_user_id=user.id)
    )

    # The resolver body is a plain instance method under the strawberry field
    # wrapper; invoke it through the wrapper's underlying resolver.
    resolver = EventAttendanceGraphQLType.__dict__["membership"].base_resolver.wrapped_func
    resolved = resolver(attendance)
    assert resolved is not None
    assert resolved.user_id == user.id
    assert resolved.organization_id == organization.id
    assert resolved.role == OrganizationRole.MEMBER

    orphan_attendance = _make_attendance(event, organization, membership_user_id=None)
    orphan_attendance = (
        EventAttendance.objects.filter_by_organization(organization.id)
        .select_related("membership")
        .get(pk=orphan_attendance.pk)
    )
    assert resolver(orphan_attendance) is None
