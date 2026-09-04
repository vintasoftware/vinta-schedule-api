"""CalendarManagementToken.minted_by_membership raw-SQL composite SET NULL FK.

``minted_by_membership`` is a ``OrganizationMembershipForeignKey`` (a Django
``ForeignObject``, no DB constraint of its own). Integrity is enforced by the
raw-SQL composite FK added in migration 0051:

    (minted_by_membership_user_id, organization_id) ->
        OrganizationMembership(user_id, organization_id)
    ON DELETE SET NULL (minted_by_membership_user_id)
    DEFERRABLE INITIALLY DEFERRED

Unlike the sibling ``membership`` FK (``calmgmttoken_membership_protect_fk``,
tested in ``test_management_token_protect_fk.py``), this one is SET NULL, not
PROTECT: deleting a user who once minted a code must not be blocked -- minting
is a far more routine event than owning a calendar. The column-list form (only
``minted_by_membership_user_id`` is nulled, never ``organization_id``, the
table's own tenant key) is the reason this needs Postgres 15+ -- see the
migration's docstring.

These tests exercise the DB constraint directly, so deferred-check assertions
run with ``check_deferred_constraints_now()`` inside the ordinary rolled-back
test transaction (see that helper's docstring for why this reaches the same
point a real ``COMMIT`` would without the teardown cost of
``transaction=True``).
"""

from __future__ import annotations

from django.db import IntegrityError, connection, transaction

import pytest
from model_bakery import baker

from calendar_integration.constants import EventManagementPermissions
from calendar_integration.models import Calendar, CalendarManagementToken
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from common.deferred_constraint_test_support import check_deferred_constraints_now
from organizations.models import Organization, OrganizationMembership
from users.models import User


CONSTRAINT_NAME = "calmgmttoken_minter_membership_fk"


@pytest.fixture
def organization(db) -> Organization:
    return baker.make(Organization)


@pytest.fixture
def minter_user(organization) -> User:
    user = baker.make(User)
    OrganizationMembership.objects.create(user=user, organization=organization)
    return user


@pytest.fixture
def calendar(organization) -> Calendar:
    return baker.make(Calendar, organization=organization)


@pytest.fixture
def service() -> CalendarPermissionService:
    return CalendarPermissionService()


def _mint_token(service, organization, calendar, minter_user) -> CalendarManagementToken:
    token, _code = service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
        minted_by_user=minter_user,
    )
    return token


# ---------------------------------------------------------------------------
# SET NULL — deleting the minting user (or its membership) nulls the column,
# leaves the token row live
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_delete_user_with_minted_token_nulls_minter_and_keeps_token_live(
    service, organization, calendar, minter_user
):
    """Deleting the User cascades to its membership; the SET NULL FK nulls
    ``minted_by_membership_user_id`` rather than blocking the delete or the
    token row disappearing."""
    token = _mint_token(service, organization, calendar, minter_user)
    assert token.minted_by_membership_user_id == minter_user.id

    minter_user.delete()
    check_deferred_constraints_now()

    token.refresh_from_db()
    assert token.minted_by_membership_user_id is None
    # The token itself is untouched otherwise -- still live, still usable.
    assert token.revoked_at is None
    assert token.used_at is None


@pytest.mark.django_db
def test_delete_membership_directly_nulls_minter(service, organization, calendar, minter_user):
    """Deleting the OrganizationMembership row directly (not via User) also nulls it."""
    token = _mint_token(service, organization, calendar, minter_user)
    membership = OrganizationMembership.objects.get(user=minter_user, organization=organization)

    membership.delete()
    check_deferred_constraints_now()

    token.refresh_from_db()
    assert token.minted_by_membership_user_id is None


@pytest.mark.django_db
def test_delete_organization_cascade_with_minted_token_succeeds(
    service, organization, calendar, minter_user
):
    """Deleting an Organization with a minted token cascades cleanly.

    Regression guard for the deferred design: Django's Python cascade collector
    is blind to the ``minted_by_membership`` ``ForeignObject`` and may delete the
    ``OrganizationMembership`` row before (or in the same transaction as) the
    ``CalendarManagementToken`` rows that reference it. Because the constraint is
    ``DEFERRABLE INITIALLY DEFERRED``, the check runs once at COMMIT -- by which
    point the whole cascade (membership, token, calendar, organization) is
    already gone -- so nothing aborts. This is the same hazard 0026 documents
    for the sibling PROTECT FKs; it applies identically here even though this
    FK's action is SET NULL, not PROTECT, because the hazard is about ordering
    the collector chooses, not about which action eventually fires.
    """
    token = _mint_token(service, organization, calendar, minter_user)
    membership = OrganizationMembership.objects.get(user=minter_user, organization=organization)

    organization.delete()  # must not raise IntegrityError
    check_deferred_constraints_now()

    assert not Organization.objects.filter(pk=organization.pk).exists()
    assert not OrganizationMembership.objects.filter(pk=membership.pk).exists()
    assert not CalendarManagementToken.original_manager.filter(pk=token.pk).exists()


# ---------------------------------------------------------------------------
# FK enforcement — non-NULL minted_by_membership_user_id must reference a
# membership; NULL is always allowed (MATCH SIMPLE)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_token_with_nonexistent_minter_membership_raises(organization, calendar):
    """A non-NULL minted_by_membership_user_id without a matching membership
    violates the FK -- a token row cannot reference a (user, organization) pair
    with no membership."""
    non_member = baker.make(User)  # NOT a member of organization

    with pytest.raises(IntegrityError), transaction.atomic():
        CalendarManagementToken.objects.create(
            organization=organization,
            calendar_fk=calendar,
            minted_by_membership_user_id=non_member.id,
            token_hash="irrelevant-hash",
        )
        check_deferred_constraints_now()


@pytest.mark.django_db
def test_token_update_to_nonexistent_minter_membership_raises(
    service, organization, calendar, minter_user
):
    """Updating minted_by_membership_user_id to a non-member value violates the FK."""
    token = _mint_token(service, organization, calendar, minter_user)
    non_member = baker.make(User)

    with pytest.raises(IntegrityError), transaction.atomic():
        CalendarManagementToken.original_manager.filter(pk=token.pk).update(
            minted_by_membership_user_id=non_member.id
        )
        check_deferred_constraints_now()


@pytest.mark.django_db
def test_null_minter_token_allowed(organization, calendar):
    """minted_by_membership_user_id=NULL (every code minted today, and every
    SystemUser-minted or internal-flow code) is allowed -- the composite FK does
    not constrain NULLs (MATCH SIMPLE)."""
    token = CalendarManagementToken.objects.create(
        organization=organization,
        calendar_fk=calendar,
        minted_by_membership_user_id=None,
        token_hash="irrelevant-hash",
    )
    check_deferred_constraints_now()

    assert token.minted_by_membership_user_id is None


# ---------------------------------------------------------------------------
# Constraint introspection — column-list SET NULL, deferrable, deferred
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_constraint_is_deferrable_initially_deferred_column_list_set_null():
    """pg_constraint shows condeferrable=t, condeferred=t, confdeltype='n' (SET
    NULL), and the constraint definition nulls ONLY minted_by_membership_user_id
    -- never organization_id, the table's own NOT NULL tenant key. Also asserts
    convalidated=t: the migration's ``NOT VALID`` / ``VALIDATE CONSTRAINT`` split
    is a lock-avoidance technique, not a way to ship an unvalidated constraint --
    the acceptance line requires the constraint be validated."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT condeferrable, condeferred, confdeltype, convalidated,
                   pg_get_constraintdef(oid)
            FROM   pg_constraint
            WHERE  conname = %s
            """,
            [CONSTRAINT_NAME],
        )
        row = cursor.fetchone()

    assert row is not None, f"constraint {CONSTRAINT_NAME} not found"
    condeferrable, condeferred, confdeltype, convalidated, definition = row
    assert condeferrable is True
    assert condeferred is True
    assert confdeltype == "n"  # 'n' = SET NULL
    assert convalidated is True
    assert "ON DELETE SET NULL (minted_by_membership_user_id)" in definition
    # The column-list form must name only the minter column -- a bare
    # ``ON DELETE SET NULL`` (no column list) would null organization_id too.
    assert "SET NULL (minted_by_membership_user_id, organization_id)" not in definition
