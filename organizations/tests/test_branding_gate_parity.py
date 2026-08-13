"""The branding gates after Phase 4: two independent conditions, still both required.

The branding surfaces compose an **entitlement** condition
(``white_label_branding``, plus "no parent") with a **role** condition. Phase 4
of the vinta-django-orgs migration replaced the role half with
``organizations.manage_branding`` and left the entitlement half alone. The way
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

import pytest
from model_bakery import baker

from organizations.exceptions import (
    BrandingEntitlementRequiredError,
    OrganizationHasParentBrandingError,
)
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from organizations.permissions import (
    BrandingWriteGateReason,
    check_branding_read_eligibility,
    evaluate_branding_write_gate,
    is_branding_eligible_organization,
    user_administers_branding_eligible_organization,
)
from organizations.tests.helpers import make_membership
from payments.billing_constants import BillingState, Entitlement
from payments.models import BillingPlan, Subscription, SubscriptionEntitlement


User = get_user_model()

pytestmark = pytest.mark.no_auto_subscription


def _organization(*, entitled: bool, **kwargs) -> Organization:
    from django.utils import timezone

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
        entitlement_key=Entitlement.WHITE_LABEL_BRANDING,
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

    def test_the_read_gate_no_longer_admits_more_than_the_write_gate(self, unentitled):
        """The read gate used to additionally admit ``NO_SLUG``, keeping a
        read-side surface open to an organization the write gate refused. That
        reason is gone (Phase 4), so the two now admit exactly the same set --
        asserted here rather than assumed, because the read gate is what guards
        the logo-signing endpoint."""
        for organization in (unentitled, baker.make(Organization, parent=unentitled)):
            write_admits = evaluate_branding_write_gate(organization) is BrandingWriteGateReason.OK
            try:
                check_branding_read_eligibility(organization)
            except Exception:  # noqa: BLE001 -- any refusal counts as "not admitted"
                read_admits = False
            else:
                read_admits = True

            assert write_admits == read_admits is False


@pytest.mark.django_db
class TestUserAdministersBrandingEligibleOrganization:
    """The role half, which is what Phase 4 changed: ``role == ADMIN`` became
    ``organizations.manage_branding``."""

    def test_an_admin_of_an_entitled_organization_is_admitted(self, entitled):
        user = baker.make(User)
        make_membership(user=user, organization=entitled, role=OrganizationRole.ADMIN)

        assert user_administers_branding_eligible_organization(user) is True

    def test_entitled_but_unpermitted_denies(self, entitled):
        """The organization can brand; this caller may not do it."""
        user = baker.make(User)
        make_membership(user=user, organization=entitled, role=OrganizationRole.MEMBER)

        assert user_administers_branding_eligible_organization(user) is False

    def test_permitted_but_unentitled_denies(self, unentitled):
        """This caller may brand; the organization cannot."""
        user = baker.make(User)
        make_membership(user=user, organization=unentitled, role=OrganizationRole.ADMIN)

        assert user_administers_branding_eligible_organization(user) is False

    def test_a_billing_owner_is_not_a_branding_administrator(self, entitled):
        """``organization_billing_owner`` carries ``payments.manage_billing`` and
        nothing else, so the two capabilities cannot be confused for one another
        -- which they could be while both were spelled "not a plain member"."""
        user = baker.make(User)
        make_membership(
            user=user,
            organization=entitled,
            role=OrganizationRole.MEMBER,
            is_billing_owner=True,
        )

        assert user_administers_branding_eligible_organization(user) is False

    def test_a_deactivated_admin_denies(self, entitled):
        user = baker.make(User)
        make_membership(
            user=user, organization=entitled, role=OrganizationRole.ADMIN, is_active=False
        )

        assert user_administers_branding_eligible_organization(user) is False

    def test_an_admin_of_a_child_organization_denies(self, entitled):
        """Branding inside a hierarchy belongs to the reseller alone; the role
        half cannot buy past the parentless condition."""
        child = baker.make(Organization, parent=entitled, slug="child-admin-parity")
        user = baker.make(User)
        make_membership(user=user, organization=child, role=OrganizationRole.ADMIN)

        assert user_administers_branding_eligible_organization(user) is False

    def test_administering_any_one_eligible_organization_is_enough(self, entitled, unentitled):
        """The coarse, user-granular contract this callable is documented to
        have -- s3direct's signing view has no notion of an acting organization.
        Pinned so a future narrowing is a decision rather than a side effect."""
        user = baker.make(User)
        make_membership(user=user, organization=unentitled, role=OrganizationRole.ADMIN)
        make_membership(user=user, organization=entitled, role=OrganizationRole.ADMIN)

        assert user_administers_branding_eligible_organization(user) is True

    def test_it_answers_with_no_organization_bound(self, entitled):
        """The s3direct signing path binds no organization at all. A check that
        read the ambient binding would refuse every caller here; this asserts
        the callable names each membership's organization itself.

        No ``organization_context`` anywhere in this test, deliberately.
        """
        user = baker.make(User)
        make_membership(user=user, organization=entitled, role=OrganizationRole.ADMIN)

        from common.organization_context import get_current_organization

        assert get_current_organization() is None
        assert user_administers_branding_eligible_organization(user) is True

    def test_an_ungrouped_admin_denies(self, entitled):
        """The fixture-shaped membership: ``role=ADMIN`` with no groups. Refused,
        because the decision reads the permission."""
        user = baker.make(User)
        baker.make(  # groups-deliberately-absent: the point of this test
            OrganizationMembership,
            user=user,
            organization=entitled,
            role=OrganizationRole.ADMIN,
        )

        assert user_administers_branding_eligible_organization(user) is False

    def test_anonymous_and_membership_less_callers_deny(self, entitled):
        assert user_administers_branding_eligible_organization(None) is False
        assert user_administers_branding_eligible_organization(AnonymousUser()) is False
        assert user_administers_branding_eligible_organization(baker.make(User)) is False
