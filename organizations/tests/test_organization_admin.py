"""``OrganizationAdmin`` — the fourth organization-creation path (Phase 4 review
BLOCKER 4), and the ``parent`` cycle guard on its form (BLOCKER 4/3).

Before this fix, adding an Organization through Django admin created a
parent-less org with no ``Subscription``, breaking the "no plan-less state"
invariant; and ``parent`` was freely editable with no acyclicity check, which is
how a cycle that ``resolve_billing_root``'s cycle guard exists for gets created.
"""

from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

import pytest
from model_bakery import baker

from organizations.models import Organization
from payments.models import Subscription
from payments.services.subscription_service import SubscriptionService


User = get_user_model()


@pytest.fixture
def superuser():
    return User.objects.create_superuser(email="org-admin@example.com", password="adminpassword")  # noqa: S106


@pytest.fixture
def admin_client(superuser):
    client = Client()
    client.force_login(superuser)
    return client


@pytest.mark.django_db
class TestOrganizationAdminPlacesNewOrgOnDefaultPlan:
    def test_adding_an_organization_via_admin_creates_a_subscription(self, admin_client):
        add_url = reverse("admin:organizations_organization_add")

        response = admin_client.post(
            add_url,
            data={
                "name": "Admin-created Org",
                "should_sync_rooms": "",
                "external_event_update_policy": "change_request",
            },
        )

        assert response.status_code == 302
        organization = Organization.objects.get(name="Admin-created Org")
        subscription = Subscription.objects.get(organization=organization)
        assert subscription.plan.slug == "unlimited"

    def test_editing_an_existing_organization_does_not_duplicate_the_subscription(
        self, admin_client
    ):
        organization = baker.make(Organization, name="Existing Org", parent=None)
        SubscriptionService().create_subscription_for_organization(organization)
        change_url = reverse("admin:organizations_organization_change", args=[organization.pk])

        response = admin_client.post(
            change_url,
            data={
                "name": "Existing Org Renamed",
                "should_sync_rooms": "",
                "external_event_update_policy": "change_request",
            },
        )

        assert response.status_code == 302
        assert Subscription.objects.filter(organization=organization).count() == 1

    def test_toggling_can_invite_organizations_on_an_existing_child_creates_a_subscription(
        self, admin_client
    ):
        """A GraphQL-created reseller child correctly has no ``Subscription`` of
        its own (it pools against its root's). Flipping ``can_invite_organizations``
        on via admin makes it its own billing root (``is_billing_root``), and
        ``save_model`` must provision a ``Subscription`` for it on that same save
        — not just on creation (Phase 4 verification review BLOCKER).
        """
        root = baker.make(Organization, name="Root", parent=None, can_invite_organizations=True)
        SubscriptionService().create_subscription_for_organization(root)
        child = baker.make(Organization, name="Child", parent=root, can_invite_organizations=False)
        assert not Subscription.objects.filter(organization=child).exists()

        change_url = reverse("admin:organizations_organization_change", args=[child.pk])
        response = admin_client.post(
            change_url,
            data={
                "name": child.name,
                "parent": root.pk,
                "should_sync_rooms": "",
                "external_event_update_policy": "change_request",
                "can_invite_organizations": "on",
            },
        )

        assert response.status_code == 302
        child.refresh_from_db()
        assert child.can_invite_organizations is True
        assert Subscription.objects.filter(organization=child).exists()


@pytest.mark.django_db
class TestOrganizationAdminParentCycleGuard:
    def test_setting_parent_to_a_descendant_is_rejected(self, admin_client):
        grandparent = baker.make(Organization, name="Grandparent", parent=None)
        parent = baker.make(Organization, name="Parent", parent=grandparent)
        child = baker.make(Organization, name="Child", parent=parent)

        # Attempt to set grandparent.parent = child, which would create a cycle
        # grandparent -> child -> parent -> grandparent.
        change_url = reverse("admin:organizations_organization_change", args=[grandparent.pk])
        response = admin_client.post(
            change_url,
            data={
                "name": grandparent.name,
                "parent": child.pk,
                "should_sync_rooms": "",
                "external_event_update_policy": "change_request",
            },
        )

        # A validation error re-renders the form (200), it does not redirect.
        assert response.status_code == 200
        grandparent.refresh_from_db()
        assert grandparent.parent_id is None

    def test_setting_parent_to_self_is_rejected(self, admin_client):
        organization = baker.make(Organization, name="Self Parent Attempt", parent=None)

        change_url = reverse("admin:organizations_organization_change", args=[organization.pk])
        response = admin_client.post(
            change_url,
            data={
                "name": organization.name,
                "parent": organization.pk,
                "should_sync_rooms": "",
                "external_event_update_policy": "change_request",
            },
        )

        assert response.status_code == 200
        organization.refresh_from_db()
        assert organization.parent_id is None

    def test_reparenting_to_a_non_descendant_is_allowed(self, admin_client):
        other_root = baker.make(Organization, name="Other Root", parent=None)
        organization = baker.make(Organization, name="Movable Org", parent=None)

        change_url = reverse("admin:organizations_organization_change", args=[organization.pk])
        response = admin_client.post(
            change_url,
            data={
                "name": organization.name,
                "parent": other_root.pk,
                "should_sync_rooms": "",
                "external_event_update_policy": "change_request",
            },
        )

        assert response.status_code == 302
        organization.refresh_from_db()
        assert organization.parent_id == other_root.pk
