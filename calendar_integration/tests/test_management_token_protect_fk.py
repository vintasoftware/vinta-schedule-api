"""Phase 6 cutover (DB half) — CalendarManagementToken membership PROTECT FK.

After Phase 6 the legacy ``user`` column is gone and a token's internal-actor
integrity is enforced at the DB level by a raw-SQL composite FK:

    (membership_user_id, organization_id) ->
        OrganizationMembership(user_id, organization_id)
    ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED

This enforces PROTECT delete semantics on the ``membership`` ForeignObject
relation (the check fires at COMMIT, so it raises at the close of the surrounding
``transaction.atomic`` block, and a same-transaction cascade that removes both
rows still succeeds — see the org-delete test below).

There is **no** partial unique constraint: a member may legitimately hold many
management tokens (calendar-level, event-level, distinct events), so duplicate
``(scope, membership_user_id)`` rows are allowed.

These tests exercise the DB constraint directly (raising ``IntegrityError``), so
they run with ``transaction=True`` and assert inside ``transaction.atomic`` blocks
where a failed statement would otherwise poison the surrounding transaction.
"""

from __future__ import annotations

from django.db import IntegrityError, connection, transaction

import pytest
from model_bakery import baker

from calendar_integration.models import Calendar, CalendarManagementToken
from common.utils.authentication_utils import generate_long_lived_token, hash_long_lived_token
from organizations.models import Organization, OrganizationMembership
from users.models import User


CONSTRAINT_NAME = "calmgmttoken_membership_protect_fk"


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


def _make_member_token(organization, calendar, member_user) -> CalendarManagementToken:
    return CalendarManagementToken.objects.create(
        organization=organization,
        calendar_fk=calendar,
        membership_user_id=member_user.id,
        token_hash=hash_long_lived_token(generate_long_lived_token()),
    )


# ---------------------------------------------------------------------------
# PROTECT — deleting a referenced membership / user is blocked
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_delete_membership_with_live_token_is_blocked(organization, member_user, calendar):
    """Deleting an OrganizationMembership referenced by a live member token raises."""
    _make_member_token(organization, calendar, member_user)
    membership = OrganizationMembership.objects.get(user=member_user, organization=organization)

    with pytest.raises(IntegrityError), transaction.atomic():
        membership.delete()


@pytest.mark.django_db(transaction=True)
def test_delete_user_with_live_token_is_blocked(organization, member_user, calendar):
    """Deleting the User cascades to its membership, which the PROTECT FK blocks.

    Documented behaviour change introduced in Phase 6: a User holding a live member
    token can no longer be deleted while the token is live — the membership-cascade
    trips the deferred PROTECT FK at COMMIT (the close of the ``atomic`` block).
    """
    _make_member_token(organization, calendar, member_user)

    with pytest.raises(IntegrityError), transaction.atomic():
        member_user.delete()


@pytest.mark.django_db(transaction=True)
def test_delete_organization_cascade_with_member_token_succeeds(
    organization, member_user, calendar
):
    """Deleting an Organization with a member token cascades cleanly.

    Regression guard for the deferred-PROTECT design. Deleting an Organization
    CASCADEs (in one transaction) to both its OrganizationMembership rows and its
    CalendarManagementToken rows. Two things would otherwise abort that cascade:

    1. If the ``membership`` ForeignObject carried ``on_delete=PROTECT``, Django's
       Python collector would raise ``ProtectedError`` eagerly the moment it sees
       the membership being collected. The ForeignObject is wired ``DO_NOTHING``
       (PROTECT lives at the DB level only).
    2. A non-deferrable DB ``RESTRICT`` FK would fire per-statement if the collector
       deletes the membership before the token. The constraint is ``NO ACTION
       DEFERRABLE INITIALLY DEFERRED`` so the check is postponed to COMMIT — by which
       point both rows are gone — and the cascade succeeds.

    This test FAILS with either an eager ForeignObject PROTECT or a non-deferrable
    RESTRICT FK, and PASSES with the deferred-DB-only PROTECT design.
    """
    token = _make_member_token(organization, calendar, member_user)
    membership = OrganizationMembership.objects.get(user=member_user, organization=organization)

    organization.delete()  # must not raise IntegrityError

    assert not Organization.objects.filter(pk=organization.pk).exists()
    assert not OrganizationMembership.objects.filter(pk=membership.pk).exists()
    assert not CalendarManagementToken.objects.filter(pk=token.pk).exists()
    assert not Calendar.objects.filter(pk=calendar.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_delete_membership_allowed_after_token_removed(organization, member_user, calendar):
    """Once the token is gone, the membership can be deleted normally."""
    token = _make_member_token(organization, calendar, member_user)
    membership = OrganizationMembership.objects.get(user=member_user, organization=organization)

    token.delete()
    membership.delete()  # no error

    assert not OrganizationMembership.objects.filter(pk=membership.pk).exists()


# ---------------------------------------------------------------------------
# FK enforcement — non-NULL membership_user_id must reference a membership
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_token_with_nonexistent_membership_raises(organization, calendar):
    """A non-NULL membership_user_id without a matching membership violates the FK."""
    non_member = baker.make("users.User")  # NOT a member of organization

    with pytest.raises(IntegrityError), transaction.atomic():
        CalendarManagementToken.objects.create(
            organization=organization,
            calendar_fk=calendar,
            membership_user_id=non_member.id,
            token_hash=hash_long_lived_token(generate_long_lived_token()),
        )


@pytest.mark.django_db(transaction=True)
def test_token_update_to_nonexistent_membership_raises(organization, member_user, calendar):
    """Updating membership_user_id to a non-member value violates the FK."""
    token = _make_member_token(organization, calendar, member_user)
    non_member = baker.make("users.User")

    with pytest.raises(IntegrityError), transaction.atomic():
        CalendarManagementToken.original_manager.filter(pk=token.pk).update(
            membership_user_id=non_member.id
        )


@pytest.mark.django_db(transaction=True)
def test_null_membership_token_allowed(organization, calendar):
    """membership_user_id=NULL (external / null-membership token) is allowed.

    The composite FK does not constrain NULLs (MATCH SIMPLE), so external-attendee
    and null-membership tokens persist without a membership.
    """
    token = CalendarManagementToken.objects.create(
        organization=organization,
        calendar_fk=calendar,
        membership_user_id=None,
        token_hash=hash_long_lived_token(generate_long_lived_token()),
    )
    assert token.membership_user_id is None


@pytest.mark.django_db(transaction=True)
def test_multiple_member_tokens_on_calendar_allowed(organization, member_user, calendar):
    """A member may hold multiple tokens — there is no partial unique constraint."""
    first = _make_member_token(organization, calendar, member_user)
    second = _make_member_token(organization, calendar, member_user)

    assert first.pk != second.pk
    assert (
        CalendarManagementToken.objects.filter_by_organization(organization.id)
        .filter(membership_user_id=member_user.id)
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
