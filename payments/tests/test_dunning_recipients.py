"""``billing_recipients``: who receives the dunning ladder.

``billing_recipients`` is built on
``active().holding_permission("vinta_billing.manage_billing")``, so "who may write
billing" and "who is told about billing" derive from one source. The two
consumers are ``DunningService._recipient_user_ids`` and
``UsageWarningService._recipient_user_ids``.

``holding_permission`` is the package's
(``vinta_orgs.querysets.OrganizationMembershipQuerySet``), and it is **wider
than the hand-written ``groups__permissions`` filter it replaced**: it matches a
membership's *direct* ``permissions`` grant as well as the permissions its
``groups`` carry. Nothing in this repo writes that M2M today, so the widening is
inert -- but it decides who receives email containing billing state, so it is
pinned in both directions below rather than left to be discovered when something
starts writing it (``test_a_direct_manage_billing_grant_is_a_recipient_too``).

**The expectations here are literal.** Deriving them from the filter under test
would make every assertion true by construction. Each test names the users it
expects by the variable that created them, and
``_capability_groups_predicate`` restates the rule as a plain Python membership
test over an already-fetched row's group *names* -- a genuinely independent
expression of it rather than a second call into the ORM path under test.

It is named for what it does rather than for the columns it descends from, and
that is the honest name now: with ``role`` and ``is_billing_owner`` gone it can
no longer be a restatement of the flat-column rule it replaced, only of the
current one in a different idiom. Read it as an independent oracle, **not** as
parity evidence against the flat-column era -- nothing in this repo can produce
that era's state any more.

The parity class that once pinned the dual-write between the old flat-column
representation and this one is gone: there is one representation now, so there
is nothing left for two writes to keep in step. What that class actually
protected -- that a re-grouping removes a capability rather than only adding
one -- is ``TestReGroupingRemovesCapabilities`` below.
"""

from __future__ import annotations

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType

import pytest
from model_bakery import baker

from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import (
    GROUP_ORGANIZATION_ADMIN,
    GROUP_ORGANIZATION_BILLING_OWNER,
    GROUP_ORGANIZATION_MEMBER,
)
from organizations.services import assign_membership_groups
from users.models import User


def _membership(
    organization: Organization,
    *,
    groups: tuple[str, ...] = (GROUP_ORGANIZATION_MEMBER,),
    is_active: bool = True,
) -> OrganizationMembership:
    """A membership written the way every live write path writes one.

    ``OrganizationMembership.objects.create`` alone leaves the groups empty;
    ``assign_membership_groups`` is what every application path that creates or
    re-groups a membership calls. Spelled out here rather than hidden behind
    ``organizations.tests.helpers`` because the group set *is* the subject.
    """
    membership = OrganizationMembership.objects.create(
        user=baker.make(User),
        organization=organization,
        is_active=is_active,
    )
    assign_membership_groups(membership, groups)
    return membership


def _capability_groups_predicate(membership: OrganizationMembership) -> bool:
    """Active, and in ``organization_admin`` or ``organization_billing_owner``.

    Independent of the queryset under test on purpose: it walks an
    already-fetched row's group *names* instead of going back through the
    ``groups -> permissions -> codename`` join ``billing_recipients`` uses, so
    agreeing with it is evidence rather than a tautology.

    Descended from the old ``role``/``is_billing_owner`` disjunction, but
    no longer a restatement *of* it -- neither column exists on the model any
    more, so this is a second expression of the current rule rather than parity
    evidence against the previous one. See the module docstring.
    """
    names = set(membership.groups.values_list("name", flat=True))
    return membership.is_active and bool(
        names & {GROUP_ORGANIZATION_ADMIN, GROUP_ORGANIZATION_BILLING_OWNER}
    )


@pytest.mark.django_db
class TestWhoReceivesBilling:
    def test_the_expected_three_and_nobody_else(self):
        organization = baker.make(Organization, name="Recipients Co", slug="recipients-co")

        admin = _membership(organization, groups=(GROUP_ORGANIZATION_ADMIN,))
        billing_owner = _membership(organization, groups=(GROUP_ORGANIZATION_BILLING_OWNER,))
        admin_and_owner = _membership(
            organization,
            groups=(GROUP_ORGANIZATION_ADMIN, GROUP_ORGANIZATION_BILLING_OWNER),
        )
        plain_member = _membership(organization)
        inactive_admin = _membership(
            organization, groups=(GROUP_ORGANIZATION_ADMIN,), is_active=False
        )
        inactive_billing_owner = _membership(
            organization, groups=(GROUP_ORGANIZATION_BILLING_OWNER,), is_active=False
        )

        recipients = set(OrganizationMembership.objects.billing_recipients(organization.id))

        assert recipients == {admin, billing_owner, admin_and_owner}
        assert plain_member not in recipients
        assert inactive_admin not in recipients
        assert inactive_billing_owner not in recipients

    def test_it_matches_an_independent_reading_of_the_group_names(self):
        """The permission join agrees with a direct read of the group names.

        Not parity against the old flat-column fields -- those are gone, and nothing
        here can produce their state. Two independent expressions of the *current*
        rule, one through ``groups -> permissions -> codename`` and one over
        already-fetched names.
        """
        organization = baker.make(Organization, name="Parity Co", slug="parity-co")
        everyone = [
            _membership(organization, groups=(GROUP_ORGANIZATION_ADMIN,)),
            _membership(organization, groups=(GROUP_ORGANIZATION_BILLING_OWNER,)),
            _membership(
                organization,
                groups=(GROUP_ORGANIZATION_ADMIN, GROUP_ORGANIZATION_BILLING_OWNER),
            ),
            _membership(organization),
            _membership(organization, groups=(GROUP_ORGANIZATION_ADMIN,), is_active=False),
            _membership(organization, groups=(GROUP_ORGANIZATION_BILLING_OWNER,), is_active=False),
            _membership(organization, is_active=False),
        ]

        recipients = set(OrganizationMembership.objects.billing_recipients(organization.id))

        assert recipients == {
            membership for membership in everyone if _capability_groups_predicate(membership)
        }
        # And that expectation is not vacuously everything or nothing.
        assert 0 < len(recipients) < len(everyone)

    def test_it_does_not_reach_into_another_organization(self):
        organization = baker.make(Organization, name="Mine Co", slug="mine-co")
        other = baker.make(Organization, name="Theirs Co", slug="theirs-co")
        mine = _membership(organization, groups=(GROUP_ORGANIZATION_ADMIN,))
        theirs = _membership(other, groups=(GROUP_ORGANIZATION_ADMIN,))

        recipients = set(OrganizationMembership.objects.billing_recipients(organization.id))

        assert recipients == {mine}
        assert theirs not in recipients

    def test_a_membership_in_two_qualifying_groups_is_returned_once(self):
        """Two chained many-to-many joins produce one row per *path*. Without
        ``distinct()`` a membership in both qualifying groups -- which both the
        backfill and ``assign_membership_groups`` can write -- would be notified
        twice."""
        organization = baker.make(Organization, name="Dupes Co", slug="dupes-co")
        both = _membership(
            organization,
            groups=(GROUP_ORGANIZATION_ADMIN, GROUP_ORGANIZATION_BILLING_OWNER),
        )

        recipients = list(OrganizationMembership.objects.billing_recipients(organization.id))

        assert recipients == [both]

    def test_a_membership_with_no_groups_at_all_is_not_a_recipient(self):
        """A membership nothing put in a group receives nothing.

        A one-time backfill migration is what put every pre-existing row in a
        group, and ``assign_membership_groups`` is what puts every row written
        since.
        Pinned so that dependency is visible rather than assumed."""
        organization = baker.make(Organization, name="Ungrouped Co", slug="ungrouped-co")
        ungrouped = OrganizationMembership.objects.create(
            user=baker.make(User),
            organization=organization,
        )

        recipients = set(OrganizationMembership.objects.billing_recipients(organization.id))

        assert ungrouped not in recipients

    def test_a_direct_manage_billing_grant_is_a_recipient_too(self):
        """The one behavioural widening ``holding_permission`` brings.

        The filter it replaced read ``groups__permissions`` only, so a
        membership holding ``vinta_billing.manage_billing`` through the model's own
        ``permissions`` M2M could write billing and never be
        told about it. The package's ``holding_permission`` unions both sources
        -- deliberately, since under-counting is the dangerous direction for the
        last-administrator guard that reads the same method.

        Nothing writes ``membership.permissions`` yet, which is exactly why this
        needs a test: the widening is invisible in the suite otherwise, and an
        upstream change that dropped the direct half would go unnoticed until a
        dunning email stopped being sent.
        """
        organization = baker.make(Organization, name="Direct Grant Co", slug="direct-grant-co")
        manage_billing = Permission.objects.get(
            content_type__app_label="vinta_billing", codename="manage_billing"
        )

        directly_granted = _membership(organization)
        directly_granted.permissions.add(manage_billing)
        plain_member = _membership(organization)

        recipients = set(OrganizationMembership.objects.billing_recipients(organization.id))

        assert recipients == {directly_granted}
        assert plain_member not in recipients
        # ...and it is the grant doing the work, not the membership: the same
        # row is not a recipient once the permission is taken back.
        directly_granted.permissions.remove(manage_billing)
        assert set(OrganizationMembership.objects.billing_recipients(organization.id)) == set()

    def test_a_deactivated_membership_with_a_direct_grant_is_still_excluded(self):
        """``active()`` runs before ``holding_permission()``, so the direct
        grant does not smuggle a soft-deleted membership back onto the list."""
        organization = baker.make(Organization, name="Direct Gone Co", slug="direct-gone-co")
        manage_billing = Permission.objects.get(
            content_type__app_label="vinta_billing", codename="manage_billing"
        )
        deactivated = _membership(organization, is_active=False)
        deactivated.permissions.add(manage_billing)

        recipients = set(OrganizationMembership.objects.billing_recipients(organization.id))

        assert recipients == set()

    def test_a_manage_billing_from_another_app_does_not_widen_it(self):
        """A ``manage_billing`` codename declared on some *other* app's model
        grants nothing here.

        The guarantee is now
        ``vinta_orgs.querysets.filter_memberships_holding_permission``'s rather
        than this file's: it keeps each codename lookup and its
        ``content_type__app_label`` inside a single ``filter()`` call, so both
        bind to the same permission row instead of joining twice. Pinned here
        anyway because our catalog spells its permissions as
        ``app_label.codename`` strings and depends on that binding -- if it were
        ever relaxed upstream, this repo's dunning list is where it would first
        do damage.
        """
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
            user=baker.make(User),
            organization=organization,
        )
        member.groups.add(impostor_group)

        recipients = set(OrganizationMembership.objects.billing_recipients(organization.id))

        assert recipients == set()


@pytest.mark.django_db
class TestReGroupingRemovesCapabilities:
    """``assign_membership_groups`` writes a *set*, not an addition.

    ``billing_recipients`` is only correct if a demotion actually takes the
    capability away; an ``add()``-shaped writer would leave a demoted admin on
    the dunning list forever. These are what dual-write parity tests for the old
    two-representation system were really protecting, restated against the
    single representation that replaced it."""

    def test_creating_an_admin_puts_it_in_the_admin_group(self):
        organization = baker.make(Organization, name="Sync Co", slug="sync-co")

        membership = _membership(organization, groups=(GROUP_ORGANIZATION_ADMIN,))

        assert set(membership.groups.values_list("name", flat=True)) == {"organization_admin"}

    def test_demoting_an_admin_removes_the_admin_group_and_the_dunning_seat(self):
        organization = baker.make(Organization, name="Demote Co", slug="demote-co")
        membership = _membership(organization, groups=(GROUP_ORGANIZATION_ADMIN,))

        assign_membership_groups(membership, [GROUP_ORGANIZATION_MEMBER])

        assert set(membership.groups.values_list("name", flat=True)) == {"organization_member"}
        recipients = set(OrganizationMembership.objects.billing_recipients(organization.id))
        assert recipients == set()

    def test_it_leaves_an_unrelated_group_alone(self):
        organization = baker.make(Organization, name="Untouched Co", slug="untouched-co")
        unrelated = Group.objects.create(name="a_group_this_writer_does_not_own")
        membership = _membership(organization, groups=(GROUP_ORGANIZATION_ADMIN,))
        membership.groups.add(unrelated)

        assign_membership_groups(membership, [GROUP_ORGANIZATION_MEMBER])

        assert set(membership.groups.values_list("name", flat=True)) == {
            "organization_member",
            "a_group_this_writer_does_not_own",
        }
