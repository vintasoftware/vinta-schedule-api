"""The branding gates: two independent conditions, both required.

The branding surfaces compose an **entitlement** condition
(``white_label_branding``, plus "no parent") with a **role** condition. The
role half used to check ``role == ADMIN`` and was replaced with
``organizations.manage_branding``; the entitlement half was left alone. The way
that composition breaks silently is by collapsing into one condition, so the two
cases this module exists for are:

* **entitled but unpermitted** -- the organization holds the entitlement and the
  caller does not hold the permission. Must still deny.
* **permitted but unentitled** -- the caller holds the permission and the
  organization does not hold the entitlement. Must still deny.

``user_administers_branding_eligible_organization`` is the thirteenth of the
thirteen classes the parity matrix covers; it lives here rather than in
``test_permissions_parity.py`` because the entitlement axis it composes with is
this module's subject. It is the ``auth`` callable for the ``branding_logos``
S3Direct destination, so it answers "does this user administer *some* eligible
organization" -- s3direct's signing view knows nothing about an acting
organization, and, notably, **binds none**: nothing about that call path resolves
an organization, which is why the permission has to be asked with one named
explicitly.

Every organization here builds its own ``Subscription`` (``no_auto_subscription``),
because the entitlement is the variable under test.
"""

import datetime

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone

import pytest
from model_bakery import baker
from vinta_billing.constants import BillingState
from vinta_billing.models import BillingPlan, Subscription, SubscriptionEntitlement

from common.organization_context import get_current_organization
from organizations.exceptions import (
    BrandingEntitlementRequiredError,
    OrganizationHasParentBrandingError,
)
from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import (
    GROUP_ORGANIZATION_ADMIN,
    GROUP_ORGANIZATION_BILLING_OWNER,
)
from organizations.permissions import (
    BrandingWriteGateReason,
    check_branding_read_eligibility,
    evaluate_branding_write_gate,
    is_branding_eligible_organization,
    user_administers_branding_eligible_organization,
)
from organizations.tests.helpers import make_membership
from payments.seams.resource_keys import WHITE_LABEL_BRANDING


User = get_user_model()

pytestmark = pytest.mark.no_auto_subscription


def _organization(*, entitled: bool, **kwargs) -> Organization:
    organization = baker.make(Organization, **kwargs)
    now = timezone.now()
    subscription = baker.make(
        Subscription,
        organization=organization,
        plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
        billing_state=BillingState.FREE,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )
    baker.make(
        SubscriptionEntitlement,
        subscription=subscription,
        entitlement_key=WHITE_LABEL_BRANDING,
        is_enabled=entitled,
    )
    return organization


@pytest.fixture
def entitled(db):
    return _organization(entitled=True, parent=None, slug="entitled-parity")


@pytest.fixture
def unentitled(db):
    return _organization(entitled=False, parent=None, slug="unentitled-parity")


@pytest.mark.django_db
class TestTheEntitlementHalfIsUntouched:
    """The gates themselves never read a role, and still do not."""

    def test_an_entitled_parentless_organization_is_admitted(self, entitled):
        assert evaluate_branding_write_gate(entitled) is BrandingWriteGateReason.OK
        assert is_branding_eligible_organization(entitled) is True
        assert check_branding_read_eligibility(entitled) is None

    def test_an_unentitled_organization_is_refused_for_the_billing_reason(self, unentitled):
        assert evaluate_branding_write_gate(unentitled) is BrandingWriteGateReason.NOT_ENTITLED
        assert is_branding_eligible_organization(unentitled) is False
        with pytest.raises(BrandingEntitlementRequiredError):
            check_branding_read_eligibility(unentitled)

    def test_a_child_organization_is_refused_for_the_permanent_reason(self, entitled):
        child = baker.make(Organization, parent=entitled, slug="child-parity")

        assert evaluate_branding_write_gate(child) is BrandingWriteGateReason.HAS_PARENT
        assert is_branding_eligible_organization(child) is False
        with pytest.raises(OrganizationHasParentBrandingError):
            check_branding_read_eligibility(child)

    def test_the_read_gate_no_longer_admits_more_than_the_write_gate(self, entitled, unentitled):
        """The read gate used to additionally admit ``NO_SLUG``, keeping a
        read-side surface open to an organization the write gate refused. That
        reason is gone now that the slug precondition is retired, so the two now
        admit exactly the same set -- asserted here rather than assumed, because
        the read gate is what guards the logo-signing endpoint.

        Asserted **over the enum**, not over two hand-picked organizations: one
        row per ``BrandingWriteGateReason``, with the set equality above them so
        a reason added later cannot quietly go uncovered. Each row names the
        exception it expects as a literal rather than looking it up in
        ``BRANDING_GATE_EXCEPTIONS`` -- that mapping is the code under test, and
        deriving the expectation from it would make the row self-comparing.

        A previous version caught bare ``Exception`` around the read gate, so an
        unrelated ``TypeError``/``AttributeError`` regression in it would have
        recorded "did not admit" and satisfied the assertion.
        """
        refusals = {
            BrandingWriteGateReason.HAS_PARENT: (
                baker.make(Organization, parent=entitled, slug="child-read-write-parity"),
                OrganizationHasParentBrandingError,
            ),
            BrandingWriteGateReason.NOT_ENTITLED: (
                unentitled,
                BrandingEntitlementRequiredError,
            ),
        }

        assert set(refusals) == set(BrandingWriteGateReason) - {BrandingWriteGateReason.OK}, (
            "every refusal reason needs a row here, or this asserts the read/write "
            "equivalence over whichever reasons someone happened to think of"
        )

        for reason, (organization, exception) in refusals.items():
            assert evaluate_branding_write_gate(organization) is reason
            with pytest.raises(exception):
                check_branding_read_eligibility(organization)

        # The admitted direction, so "the two agree" is not satisfied by two
        # gates that refuse everybody.
        assert evaluate_branding_write_gate(entitled) is BrandingWriteGateReason.OK
        assert check_branding_read_eligibility(entitled) is None


@pytest.mark.django_db
class TestUserAdministersBrandingEligibleOrganization:
    """The role half: ``role == ADMIN`` became ``organizations.manage_branding``."""

    def test_an_admin_of_an_entitled_organization_is_admitted(self, entitled):
        user = baker.make(User)
        make_membership(user=user, organization=entitled, groups=[GROUP_ORGANIZATION_ADMIN])

        assert user_administers_branding_eligible_organization(user) is True

    def test_entitled_but_unpermitted_denies(self, entitled):
        """The organization can brand; this caller may not do it."""
        user = baker.make(User)
        make_membership(
            user=user,
            organization=entitled,
        )

        assert user_administers_branding_eligible_organization(user) is False

    def test_permitted_but_unentitled_denies(self, unentitled):
        """This caller may brand; the organization cannot."""
        user = baker.make(User)
        make_membership(user=user, organization=unentitled, groups=[GROUP_ORGANIZATION_ADMIN])

        assert user_administers_branding_eligible_organization(user) is False

    def test_a_billing_owner_is_not_a_branding_administrator(self, entitled):
        """``organization_billing_owner`` carries ``vinta_billing.manage_billing`` and
        nothing else, so the two capabilities cannot be confused for one another
        -- which they could be while both were spelled "not a plain member"."""
        user = baker.make(User)
        make_membership(
            user=user,
            organization=entitled,
            groups=[GROUP_ORGANIZATION_BILLING_OWNER],
        )

        assert user_administers_branding_eligible_organization(user) is False

    def test_a_deactivated_admin_denies(self, entitled):
        user = baker.make(User)
        make_membership(
            user=user, organization=entitled, groups=[GROUP_ORGANIZATION_ADMIN], is_active=False
        )

        assert user_administers_branding_eligible_organization(user) is False

    def test_a_deactivated_user_holding_an_active_admin_membership_denies(self, entitled):
        """The membership is live; the *user* is not.

        This used to come free: the per-membership
        ``has_organization_permission`` refused an inactive user before looking
        at anything. The queryset form filters ``membership.is_active`` only, so
        the callable checks ``user.is_active`` itself -- pinned here rather than
        left to the next reader to rediscover, since the request path cannot
        produce this shape (authentication refuses an inactive user first) and
        so would not catch its loss.
        """
        user = baker.make(User, is_active=False)
        make_membership(user=user, organization=entitled, groups=[GROUP_ORGANIZATION_ADMIN])

        assert user_administers_branding_eligible_organization(user) is False

    def test_an_admin_of_a_child_organization_denies(self, entitled):
        """Branding inside a hierarchy belongs to the reseller alone; the role
        half cannot buy past the parentless condition."""
        child = baker.make(Organization, parent=entitled, slug="child-admin-parity")
        user = baker.make(User)
        make_membership(user=user, organization=child, groups=[GROUP_ORGANIZATION_ADMIN])

        assert user_administers_branding_eligible_organization(user) is False

    def test_administering_any_one_eligible_organization_is_enough(self, entitled, unentitled):
        """The coarse, user-granular contract this callable is documented to
        have -- s3direct's signing view has no notion of an acting organization.
        Pinned so a future narrowing is a decision rather than a side effect."""
        user = baker.make(User)
        make_membership(user=user, organization=unentitled, groups=[GROUP_ORGANIZATION_ADMIN])
        make_membership(user=user, organization=entitled, groups=[GROUP_ORGANIZATION_ADMIN])

        assert user_administers_branding_eligible_organization(user) is True

    def test_the_permission_half_is_one_query_however_many_organizations(
        self, django_assert_num_queries
    ):
        """The membership half costs one query, not three per organization.

        This is the ``auth`` callable for the ``branding_logos`` S3Direct
        destination, so it runs on the logo-signing request with no acting
        organization to narrow by -- every organization the caller belongs to is
        in scope by construction. Asking
        ``has_organization_permission`` per membership cost three queries each
        (the membership, its ``permissions``, its ``groups``' permissions), so a
        caller in three organizations paid ten; ``holding_permission`` asks the
        same question of the database once. Pinned as a number rather than
        described in a comment, because the comment it replaces claimed one
        query per organization and was wrong by a factor of three.

        Every organization here is parentless **and** entitled, so the
        permission is the only thing refusing: a regression that dropped the
        ``holding_permission`` filter would both fail this count (it would go on
        to pay two entitlement queries apiece) and return ``True``.
        """
        user = baker.make(User)
        for index in range(3):
            organization = _organization(entitled=True, parent=None, slug=f"nquery-{index}")
            make_membership(user=user, organization=organization)

        with django_assert_num_queries(1):
            assert user_administers_branding_eligible_organization(user) is False

    def test_an_admitted_caller_pays_the_entitlement_half_and_nothing_more(
        self, entitled, django_assert_num_queries
    ):
        """The control for the count above: one membership query plus the two
        the entitlement half has always cost (subscription, entitlement row).

        Without it, ``1`` above is also satisfied by a callable that never
        reaches ``is_branding_eligible_organization`` at all.
        """
        user = baker.make(User)
        make_membership(user=user, organization=entitled, groups=[GROUP_ORGANIZATION_ADMIN])

        with django_assert_num_queries(3):
            assert user_administers_branding_eligible_organization(user) is True

    def test_it_answers_with_no_organization_bound(self, entitled):
        """The s3direct signing path binds no organization at all. A check that
        read the ambient binding would refuse every caller here; this asserts
        the callable names each membership's organization itself.

        No ``organization_context`` anywhere in this test, deliberately.
        """
        user = baker.make(User)
        make_membership(user=user, organization=entitled, groups=[GROUP_ORGANIZATION_ADMIN])

        assert get_current_organization() is None
        assert user_administers_branding_eligible_organization(user) is True

    def test_an_ungrouped_membership_denies(self, entitled):
        """A membership in no group at all -- what a raw ``baker.make``
        produces. Refused, because the decision reads the permission."""
        user = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=user,
            organization=entitled,
        )

        assert user_administers_branding_eligible_organization(user) is False

    def test_anonymous_and_membership_less_callers_deny(self, entitled):
        assert user_administers_branding_eligible_organization(None) is False
        assert user_administers_branding_eligible_organization(AnonymousUser()) is False
        assert user_administers_branding_eligible_organization(baker.make(User)) is False
