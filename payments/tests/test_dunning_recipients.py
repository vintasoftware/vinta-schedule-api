"""``billing_recipients`` after the switch from ``role`` to ``payments.manage_billing``.

Phase 3 replaces ``Q(role=ADMIN) | Q(is_billing_owner=True)`` with
``filter(groups__permissions__codename="manage_billing", ...)``, so "who may
write billing" and "who is told about billing" derive from one source. The two
consumers are ``DunningService._recipient_user_ids`` and
``UsageWarningService._recipient_user_ids``.

**The expectations here are literal.** Deriving them from the filter under test
-- or from the old filter re-expressed through the same queryset -- would make
every assertion true by construction and prove nothing about the switch. Each
test names the users it expects by the variable that created them, and the
parity test additionally re-computes the *old* rule as a plain Python predicate
over attributes, which is a genuinely independent expression of it rather than a
second call into the ORM path being replaced.
"""

from __future__ import annotations

from django.contrib.auth.models import Group

import pytest
from model_bakery import baker

from organizations.models import Organization, OrganizationMembership, OrganizationRole
from organizations.services import sync_membership_groups_from_role
from users.models import User


def _membership(
    organization: Organization,
    *,
    role: str = OrganizationRole.MEMBER,
    is_billing_owner: bool = False,
    is_active: bool = True,
) -> OrganizationMembership:
    """A membership written the way every live write path writes one.

    ``OrganizationMembership.objects.create`` alone leaves the groups empty --
    the dual-write in ``organizations.services`` is what keeps them in step with
    ``role`` / ``is_billing_owner`` until Phase 6 retires the columns, and every
    application path that creates or re-roles a membership calls it.
    """
    membership = OrganizationMembership.objects.create(
        user=baker.make(User),
        organization=organization,
        role=role,
        is_billing_owner=is_billing_owner,
        is_active=is_active,
    )
    sync_membership_groups_from_role(membership)
    return membership


def _old_rule(membership: OrganizationMembership) -> bool:
    """The pre-Phase-3 rule, as a plain predicate over the two columns.

    Independent of the queryset under test on purpose: it reads attributes on an
    already-fetched row instead of going back through the ORM, so agreeing with
    it is evidence rather than a tautology.
    """
    return membership.is_active and (
        membership.role == OrganizationRole.ADMIN or membership.is_billing_owner
    )


@pytest.mark.django_db
class TestWhoReceivesBilling:
    def test_the_expected_three_and_nobody_else(self):
        organization = baker.make(Organization, name="Recipients Co", slug="recipients-co")

        admin = _membership(organization, role=OrganizationRole.ADMIN)
        billing_owner = _membership(organization, is_billing_owner=True)
        admin_and_owner = _membership(
            organization, role=OrganizationRole.ADMIN, is_billing_owner=True
        )
        plain_member = _membership(organization)
        inactive_admin = _membership(organization, role=OrganizationRole.ADMIN, is_active=False)
        inactive_billing_owner = _membership(organization, is_billing_owner=True, is_active=False)

        recipients = set(OrganizationMembership.objects.billing_recipients(organization.id))

        assert recipients == {admin, billing_owner, admin_and_owner}
        assert plain_member not in recipients
        assert inactive_admin not in recipients
        assert inactive_billing_owner not in recipients

    def test_it_matches_the_rule_it_replaced(self):
        """Parity, checked against the old rule expressed independently."""
        organization = baker.make(Organization, name="Parity Co", slug="parity-co")
        everyone = [
            _membership(organization, role=OrganizationRole.ADMIN),
            _membership(organization, is_billing_owner=True),
            _membership(organization, role=OrganizationRole.ADMIN, is_billing_owner=True),
            _membership(organization),
            _membership(organization, role=OrganizationRole.ADMIN, is_active=False),
            _membership(organization, is_billing_owner=True, is_active=False),
            _membership(organization, is_active=False),
        ]

        recipients = set(OrganizationMembership.objects.billing_recipients(organization.id))

        assert recipients == {membership for membership in everyone if _old_rule(membership)}
        # And that expectation is not vacuously everything or nothing.
        assert 0 < len(recipients) < len(everyone)

    def test_it_does_not_reach_into_another_organization(self):
        organization = baker.make(Organization, name="Mine Co", slug="mine-co")
        other = baker.make(Organization, name="Theirs Co", slug="theirs-co")
        mine = _membership(organization, role=OrganizationRole.ADMIN)
        theirs = _membership(other, role=OrganizationRole.ADMIN)

        recipients = set(OrganizationMembership.objects.billing_recipients(organization.id))

        assert recipients == {mine}
        assert theirs not in recipients

    def test_a_membership_in_two_qualifying_groups_is_returned_once(self):
        """Two chained many-to-many joins produce one row per *path*. Without
        ``distinct()`` an admin who is also flagged ``is_billing_owner`` -- which
        is exactly what the backfill writes for that state -- would be notified
        twice."""
        organization = baker.make(Organization, name="Dupes Co", slug="dupes-co")
        both = _membership(organization, role=OrganizationRole.ADMIN, is_billing_owner=True)

        recipients = list(OrganizationMembership.objects.billing_recipients(organization.id))

        assert recipients == [both]

    def test_a_membership_with_no_groups_at_all_is_not_a_recipient(self):
        """The failure mode the dual-write and the backfill exist to prevent:
        ``role=ADMIN`` on a row nothing put in a group buys nothing now. Pinned
        so the dependency is visible rather than assumed."""
        organization = baker.make(Organization, name="Ungrouped Co", slug="ungrouped-co")
        ungrouped = OrganizationMembership.objects.create(
            user=baker.make(User), organization=organization, role=OrganizationRole.ADMIN
        )

        recipients = set(OrganizationMembership.objects.billing_recipients(organization.id))

        assert ungrouped not in recipients

    def test_a_manage_billing_from_another_app_does_not_widen_it(self):
        """The query matches ``content_type__app_label`` alongside the codename,
        and both lookups sit in one ``filter()`` call so they bind to the same
        permission row. A ``manage_billing`` declared elsewhere therefore grants
        nothing here."""
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType

        organization = baker.make(Organization, name="Impostor Co", slug="impostor-co")
        impostor_content_type = ContentType.objects.get(
            app_label="organizations", model="organizationinvitation"
        )
        impostor = Permission.objects.create(
            content_type=impostor_content_type,
            codename="manage_billing",
            name="An unrelated manage_billing",
        )
        impostor_group = Group.objects.create(name="impostor_billing_group")
        impostor_group.permissions.add(impostor)

        member = OrganizationMembership.objects.create(
            user=baker.make(User), organization=organization, role=OrganizationRole.MEMBER
        )
        member.groups.add(impostor_group)

        recipients = set(OrganizationMembership.objects.billing_recipients(organization.id))

        assert recipients == set()


@pytest.mark.django_db
class TestTheDualWriteKeepsTheTwoRepresentationsInStep:
    """``billing_recipients`` is only equivalent to the old rule while the groups
    track ``role`` / ``is_billing_owner``. These pin the shim that makes that
    true -- all of which Phase 6 deletes together with the columns."""

    def test_creating_an_admin_puts_it_in_the_admin_group(self):
        organization = baker.make(Organization, name="Sync Co", slug="sync-co")

        membership = _membership(organization, role=OrganizationRole.ADMIN)

        assert set(membership.groups.values_list("name", flat=True)) == {"organization_admin"}

    def test_demoting_an_admin_removes_the_admin_group(self):
        organization = baker.make(Organization, name="Demote Co", slug="demote-co")
        membership = _membership(organization, role=OrganizationRole.ADMIN)

        membership.role = OrganizationRole.MEMBER
        membership.save(update_fields=["role"])
        sync_membership_groups_from_role(membership)

        assert set(membership.groups.values_list("name", flat=True)) == {"organization_member"}
        recipients = set(OrganizationMembership.objects.billing_recipients(organization.id))
        assert recipients == set()

    def test_it_leaves_an_unrelated_group_alone(self):
        organization = baker.make(Organization, name="Untouched Co", slug="untouched-co")
        unrelated = Group.objects.create(name="a_group_this_shim_does_not_own")
        membership = _membership(organization, role=OrganizationRole.ADMIN)
        membership.groups.add(unrelated)

        membership.role = OrganizationRole.MEMBER
        membership.save(update_fields=["role"])
        sync_membership_groups_from_role(membership)

        assert set(membership.groups.values_list("name", flat=True)) == {
            "organization_member",
            "a_group_this_shim_does_not_own",
        }
