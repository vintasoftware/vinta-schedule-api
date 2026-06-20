"""Phase 4b cutover (DB half) — EventAttendance membership PROTECT FK.

After Phase 4b the legacy ``user`` column is gone and attendance integrity is
enforced at the DB level by a raw-SQL composite FK ``(membership_user_id,
organization_id) -> OrganizationMembership(user_id, organization_id) ON DELETE NO
ACTION DEFERRABLE INITIALLY DEFERRED`` (constraint
``evattendance_membership_protect_fk``). The check fires at COMMIT, so it raises
at the close of the surrounding ``transaction.atomic`` block, and a
same-transaction cascade that removes both rows still succeeds (see the org-delete
test below).

No unique constraint is added on ``(event, membership_user_id)`` — EventAttendance
has no ``update_or_create`` that needs one and a new unique could fail on
pre-existing duplicate attendances.

These tests exercise the DB constraint directly (raising ``IntegrityError``), so
they run with ``transaction=True`` and assert inside ``transaction.atomic`` blocks
where a failed statement would otherwise poison the surrounding transaction.
"""

from __future__ import annotations

import datetime

from django.db import IntegrityError, connection, transaction

import pytest
from model_bakery import baker

from calendar_integration.factories import create_event_attendance
from calendar_integration.models import CalendarEvent, EventAttendance
from organizations.models import Organization, OrganizationMembership
from users.models import User


CONSTRAINT_NAME = "evattendance_membership_protect_fk"


@pytest.fixture
def organization(db) -> Organization:
    return baker.make(Organization)


@pytest.fixture
def member_user(organization) -> User:
    user = baker.make("users.User")
    OrganizationMembership.objects.create(user=user, organization=organization)
    return user


@pytest.fixture
def event(organization) -> CalendarEvent:
    return baker.make(
        CalendarEvent,
        organization=organization,
        title="Test Event",
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 10, 0, tzinfo=datetime.UTC),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
    )


# ---------------------------------------------------------------------------
# PROTECT — deleting a referenced membership / user is blocked
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_delete_membership_with_live_attendance_is_blocked(organization, member_user, event):
    """Deleting an OrganizationMembership referenced by a live attendance raises IntegrityError."""
    create_event_attendance(event=event, user=member_user)
    membership = OrganizationMembership.objects.get(user=member_user, organization=organization)

    with pytest.raises(IntegrityError), transaction.atomic():
        membership.delete()


@pytest.mark.django_db(transaction=True)
def test_delete_user_with_live_attendance_is_blocked(organization, member_user, event):
    """Deleting the User cascades to its membership, which the PROTECT FK blocks.

    Documented behaviour change introduced in Phase 4b: a User attending an event
    through their membership can no longer be deleted while the attendance is live —
    the membership-cascade trips the deferred PROTECT FK at COMMIT (the close of the
    ``transaction.atomic`` block).
    """
    create_event_attendance(event=event, user=member_user)

    with pytest.raises(IntegrityError), transaction.atomic():
        member_user.delete()


@pytest.mark.django_db(transaction=True)
def test_delete_organization_cascade_with_member_attendance_succeeds(
    organization, member_user, event
):
    """Deleting an Organization with a member attendance cascades cleanly.

    Regression guard for the deferred-PROTECT design. Deleting an Organization
    CASCADEs (in one transaction) to both its OrganizationMembership rows and its
    EventAttendance rows. Two things would otherwise abort that cascade:

    1. If the ``membership`` ForeignObject carried ``on_delete=PROTECT``, Django's
       *Python* collector would raise ``ProtectedError`` eagerly the moment it sees
       the membership being collected — even though the referencing attendance is
       being removed in the same transaction. The ForeignObject is therefore wired
       ``DO_NOTHING`` (PROTECT lives at the DB level only).
    2. A non-deferrable DB ``RESTRICT`` FK would fire per-statement if the collector
       deletes the membership before the attendance. The constraint is ``NO ACTION
       DEFERRABLE INITIALLY DEFERRED`` so the check is postponed to COMMIT — by which
       point both rows are gone — and the cascade succeeds.

    This test FAILS with either an eager ForeignObject PROTECT or a non-deferrable
    RESTRICT FK, and PASSES with the deferred-DB-only PROTECT design.
    """
    attendance = create_event_attendance(event=event, user=member_user)
    membership = OrganizationMembership.objects.get(user=member_user, organization=organization)

    organization.delete()  # must not raise IntegrityError

    assert not Organization.objects.filter(pk=organization.pk).exists()
    assert not OrganizationMembership.objects.filter(pk=membership.pk).exists()
    assert not EventAttendance.objects.filter(pk=attendance.pk).exists()
    assert not CalendarEvent.objects.filter(pk=event.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_delete_membership_allowed_after_attendance_removed(organization, member_user, event):
    """Once the attendance is gone, the membership can be deleted normally."""
    attendance = create_event_attendance(event=event, user=member_user)
    membership = OrganizationMembership.objects.get(user=member_user, organization=organization)

    attendance.delete()
    membership.delete()  # no error

    assert not OrganizationMembership.objects.filter(pk=membership.pk).exists()


# ---------------------------------------------------------------------------
# FK enforcement — non-NULL membership_user_id must reference a membership
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_attendance_with_nonexistent_membership_raises(organization, event):
    """A non-NULL membership_user_id without a matching membership violates the FK."""
    non_member = baker.make("users.User")  # NOT a member of organization

    with pytest.raises(IntegrityError), transaction.atomic():
        EventAttendance.objects.create(
            organization=organization,
            event=event,
            membership_user_id=non_member.id,
        )


@pytest.mark.django_db(transaction=True)
def test_attendance_update_to_nonexistent_membership_raises(organization, member_user, event):
    """Updating membership_user_id to a non-member value violates the FK."""
    attendance = create_event_attendance(event=event, user=member_user)
    non_member = baker.make("users.User")

    with pytest.raises(IntegrityError), transaction.atomic():
        EventAttendance.original_manager.filter(pk=attendance.pk).update(
            membership_user_id=non_member.id
        )


@pytest.mark.django_db(transaction=True)
def test_orphan_attendance_null_membership_allowed(organization, event):
    """membership_user_id=NULL (orphan) is allowed — the FK does not constrain NULLs."""
    attendance = EventAttendance.objects.create(
        organization=organization,
        event=event,
        membership_user_id=None,
    )
    assert attendance.membership_user_id is None


@pytest.mark.django_db(transaction=True)
def test_multiple_orphan_attendances_on_event_allowed(organization, event):
    """Two NULL (orphan) attendances on the same event are allowed (no unique constraint)."""
    first = EventAttendance.objects.create(
        organization=organization,
        event=event,
        membership_user_id=None,
    )
    second = EventAttendance.objects.create(
        organization=organization,
        event=event,
        membership_user_id=None,
    )
    assert first.pk != second.pk
    assert (
        EventAttendance.objects.filter_by_organization(organization.id)
        .filter(event=event, membership_user_id__isnull=True)
        .count()
        == 2
    )


# ---------------------------------------------------------------------------
# Constraint introspection — deferrable / deferred / NO ACTION
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_protect_fk_is_deferrable_initially_deferred_no_action():
    """pg_constraint shows condeferrable=t, condeferred=t, confdeltype='a' (NO ACTION)."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT condeferrable, condeferred, confdeltype
            FROM   pg_constraint
            WHERE  conname = %s
            """,
            [CONSTRAINT_NAME],
        )
        row = cursor.fetchone()

    assert row is not None, f"constraint {CONSTRAINT_NAME} not found"
    condeferrable, condeferred, confdeltype = row
    assert condeferrable is True
    assert condeferred is True
    assert confdeltype == "a"  # 'a' = NO ACTION
