"""Every organization always has exactly one active plan, from creation — no
plan-less state (billing plans and limits plan, Phase 4).

There are exactly four organization-creation paths in the codebase:

1. ``OrganizationService.create_organization`` — the REST funnel
   (``organizations/serializers.py``'s ``OrganizationSerializer.create``) and the
   ``organization_name`` branch of ``provision_tenant_for_user`` both delegate here.
2. ``OrganizationService.provision_tenant_for_user``'s invite branch — joins an
   *existing* organization (creates a membership only, no new ``Organization`` row),
   so it never needs to place anything on a plan itself.
3. The reseller GraphQL mutation (``public_api.mutations.create_organization``) — a
   raw ``Organization.objects.create(...)`` that bypasses ``OrganizationService``
   entirely. Its child always has ``parent`` set, so it correctly resolves to its
   root's subscription rather than getting one of its own.
4. Django admin (``organizations.admin.OrganizationAdmin``) — a fourth, previously
   unhooked path (Phase 4 review BLOCKER 4). ``save_model`` now places a newly
   created organization on the default plan the same way path 1 does.

This file drives all four (plus the reseller-child no-subscription case, a
nested-reseller-is-its-own-billing-root case, and a cyclic-tree case) against
real objects — no mocked subscription creation — since a missed hook here is
exactly the kind of thing that leaves half the organizations plan-less.
"""

from django.contrib.auth import get_user_model
from django.test import Client as DjangoClient
from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from organizations.models import Organization
from organizations.services import OrganizationService
from payments.exceptions import BillingRootCycleError
from payments.models import Subscription
from payments.services.subscription_service import (
    SubscriptionService,
    billing_root_filter,
    is_billing_root,
    resolve_billing_root,
)
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


@pytest.fixture
def user():
    return baker.make(get_user_model(), email="creator@example.com")


@pytest.mark.django_db
class TestRestOrganizationCreationGetsASubscription:
    def test_post_organizations_places_new_org_on_default_plan(self, user):
        client = APIClient()
        client.force_authenticate(user=user)

        url = reverse("api:Organizations-list")
        response = client.post(url, {"name": "REST Org"}, format="json")

        assert response.status_code == 201
        organization = Organization.objects.get(name="REST Org")

        subscription = Subscription.objects.get(organization=organization)
        assert subscription.plan.slug == "unlimited"


@pytest.mark.django_db
class TestProvisionTenantForUserGetsASubscription:
    def test_organization_name_branch_places_new_org_on_default_plan(self, user):
        service = OrganizationService()
        membership = service.provision_tenant_for_user(user=user, organization_name="Signup Org")

        assert membership is not None
        subscription = Subscription.objects.get(organization=membership.organization)
        assert subscription.plan.slug == "unlimited"

    def test_invite_branch_does_not_touch_subscriptions(self, user):
        """Joining an *existing* org via a pending invitation creates a membership
        only — it must not attempt (and does not need) to place a plan, since the
        org already has one from when it was created."""
        from datetime import UTC, datetime, timedelta

        from organizations.models import OrganizationInvitation, OrganizationRole
        from organizations.services import OrganizationService as _OrganizationService

        creator = baker.make(get_user_model(), email="org-owner@example.com")
        organization = _OrganizationService().create_organization(
            creator=creator, name="Existing Org"
        )
        existing_subscription = Subscription.objects.get(organization=organization)

        baker.make(
            OrganizationInvitation,
            email=user.email,
            organization=organization,
            role=OrganizationRole.MEMBER,
            expires_at=datetime.now(tz=UTC) + timedelta(days=7),
        )

        membership = _OrganizationService().provision_tenant_for_user(user=user)

        assert membership is not None
        assert membership.organization_id == organization.id
        # No second subscription was created for the org the user joined.
        assert Subscription.objects.filter(organization=organization).count() == 1
        assert Subscription.objects.get(organization=organization).pk == existing_subscription.pk


@pytest.mark.django_db
class TestResellerGraphQLMutationOrganizationCreation:
    def _create_system_user_with_org_access(self, organization):
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="test_integration", organization=organization
        )
        baker.make(ResourceAccess, system_user=system_user, resource_name="organization")
        return system_user, token

    def test_child_organization_gets_no_subscription_but_resolves_to_root(self):
        from di_core.containers import container

        reseller_org = baker.make(Organization, name="Reseller Org", can_invite_organizations=True)
        system_user, token = self._create_system_user_with_org_access(reseller_org)

        mutation = """
        mutation CreateOrganization($input: CreateOrganizationInput!) {
            createOrganization(input: $input) {
                organization { id name }
            }
        }
        """

        client = APIClient()
        auth_service = PublicAPIAuthService()
        with container.public_api_auth_service.override(auth_service):
            response = client.post(
                "/graphql/",
                data={"query": mutation, "variables": {"input": {"name": "Child Org"}}},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data, data
        child_org = Organization.objects.get(name="Child Org")
        assert child_org.parent_id == reseller_org.id

        # The plan's core invariant: a reseller child never gets its own
        # subscription — it pools against its root's.
        assert not Subscription.objects.filter(organization=child_org).exists()
        assert resolve_billing_root(child_org) == reseller_org


@pytest.mark.django_db
class TestReseleverMutationSubscriptionHookIsDefenseInDepth:
    """``public_api.mutations.create_organization`` always creates its child with
    ``parent=acting_org`` and ``can_invite_organizations=False``, so its
    ``create_subscription_for_organization(child_org)`` call is a no-op under
    every input the mutation can currently produce (see the inline comment at
    the call site). This is not dead code, though: the exact same call would
    correctly place a subscription on a hypothetical future child that somehow
    ended up a billing root (parent-less, or itself a reseller) — proven here
    directly against ``SubscriptionService``, which is what the mutation calls.
    """

    def test_would_place_a_subscription_on_a_hypothetical_billing_root_child(self):
        would_be_child = baker.make(Organization, parent=None, can_invite_organizations=False)

        subscription = SubscriptionService().create_subscription_for_organization(would_be_child)

        assert subscription is not None
        assert subscription.organization == would_be_child


@pytest.mark.django_db
class TestResolveBillingRootTreeShapes:
    def test_three_level_reseller_tree_resolves_to_root(self):
        root = baker.make(Organization, can_invite_organizations=True)
        mid = baker.make(Organization, parent=root, can_invite_organizations=False)
        leaf = baker.make(Organization, parent=mid, can_invite_organizations=False)

        assert resolve_billing_root(leaf) == root
        assert resolve_billing_root(mid) == root
        assert resolve_billing_root(root) == root

    def test_nested_reseller_is_its_own_billing_root(self):
        """A nested reseller (``can_invite_organizations=True`` with a ``parent``
        set) is its own billing root — it does not pool against its parent's
        subscription, unlike a plain child (BLOCKER 1, Phase 4 review)."""
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        mid = baker.make(Organization, parent=root, can_invite_organizations=True)
        leaf = baker.make(Organization, parent=mid, can_invite_organizations=False)

        assert is_billing_root(mid) is True
        assert resolve_billing_root(mid) == mid
        assert resolve_billing_root(leaf) == mid

        SubscriptionService().create_subscription_for_organization(root)
        mid_subscription = SubscriptionService().create_subscription_for_organization(mid)

        assert mid_subscription is not None
        assert mid_subscription.organization == mid
        assert not Subscription.objects.filter(organization=leaf).exists()

    def test_cyclic_parent_chain_raises_billing_root_cycle_error(self):
        org_a = baker.make(Organization, can_invite_organizations=False)
        org_b = baker.make(Organization, parent=org_a, can_invite_organizations=False)
        org_a.parent = org_b
        org_a.save(update_fields=["parent"])

        # Must raise a named error rather than returning an arbitrary node from
        # the cycle (BLOCKER 3, Phase 4 review) — the previous assertion
        # (`result.pk in (org_a.pk, org_b.pk)`) passed while the invariant was
        # broken: every organization on the cycle was left without a resolvable
        # billing root.
        with pytest.raises(BillingRootCycleError):
            resolve_billing_root(org_a)


@pytest.mark.django_db
class TestNoPlanlessOrganization:
    def test_every_root_organization_has_a_subscription_after_the_four_paths(self, user):
        from di_core.containers import container

        # Path 1: REST.
        client = APIClient()
        client.force_authenticate(user=user)
        client.post(reverse("api:Organizations-list"), {"name": "REST Org 2"}, format="json")

        # Path 2: provision_tenant_for_user (signup, org-name branch).
        signup_user = baker.make(get_user_model(), email="signup@example.com")
        OrganizationService().provision_tenant_for_user(
            user=signup_user, organization_name="Signup Org 2"
        )

        # Path 4: Django admin. Also how the reseller root below is created --
        # can_invite_organizations is "DB/Django-admin only, never exposed via
        # any API" (organizations/models.py), so admin is the only real path
        # that can produce one. Exercising it here (rather than baker.make +
        # manually creating its subscription) is what makes this test exercise
        # OrganizationAdmin.save_model's hook instead of asserting a tautology.
        superuser = baker.make(
            get_user_model(), email="admin2@example.com", is_staff=True, is_superuser=True
        )
        admin_client = DjangoClient()
        admin_client.force_login(superuser)
        admin_client.post(
            reverse("admin:organizations_organization_add"),
            data={
                "name": "Reseller Org 2",
                "should_sync_rooms": "",
                "external_event_update_policy": "change_request",
                "can_invite_organizations": "on",
            },
        )
        reseller_org = Organization.objects.get(name="Reseller Org 2")

        # Path 3: reseller GraphQL mutation.
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="test_integration", organization=reseller_org
        )
        baker.make(ResourceAccess, system_user=system_user, resource_name="organization")
        mutation = """
        mutation CreateOrganization($input: CreateOrganizationInput!) {
            createOrganization(input: $input) { organization { id name } }
        }
        """
        with container.public_api_auth_service.override(auth_service):
            client.post(
                "/graphql/",
                data={"query": mutation, "variables": {"input": {"name": "Child Org 2"}}},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert (
            Organization.objects.filter(billing_root_filter(), subscription__isnull=True).count()
            == 0
        )
