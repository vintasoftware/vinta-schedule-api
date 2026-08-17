"""``OrganizationAdmin`` — the fourth organization-creation path, and the
``parent`` cycle check on its form.

Before this fix, adding an Organization through Django admin created a
parent-less org with no ``Subscription``, breaking the "no plan-less state"
rule; and ``parent`` was freely editable with no acyclicity check, which is
how a cycle that ``resolve_billing_root``'s cycle check exists for gets created.
"""

from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

import pytest
from model_bakery import baker

from organizations.models import Organization, OrganizationBranding
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
                "week_start": "monday",
            },
        )

        assert response.status_code == 302
        organization = Organization.objects.get(name="Admin-created Org")
        subscription = Subscription.objects.get(organization=organization)
        assert subscription.plan.slug == "unlimited"

    def test_editing_an_existing_organization_does_not_duplicate_the_subscription(
        self, admin_client
    ):
        organization = baker.make(
            Organization, name="Existing Org", parent=None, slug="existing-org"
        )
        SubscriptionService().create_subscription_for_organization(organization)
        change_url = reverse("admin:organizations_organization_change", args=[organization.pk])

        response = admin_client.post(
            change_url,
            data={
                "name": "Existing Org Renamed",
                "slug": organization.slug,
                "should_sync_rooms": "",
                "external_event_update_policy": "change_request",
                "week_start": "monday",
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
        — not just on creation.
        """
        root = baker.make(Organization, name="Root", parent=None, can_invite_organizations=True)
        SubscriptionService().create_subscription_for_organization(root)
        child = baker.make(
            Organization,
            name="Child",
            parent=root,
            can_invite_organizations=False,
            slug="child-org",
        )
        assert not Subscription.objects.filter(organization=child).exists()

        change_url = reverse("admin:organizations_organization_change", args=[child.pk])
        response = admin_client.post(
            change_url,
            data={
                "name": child.name,
                "slug": child.slug,
                "parent": root.pk,
                "should_sync_rooms": "",
                "external_event_update_policy": "change_request",
                "week_start": "monday",
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
                "week_start": "monday",
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
                "week_start": "monday",
            },
        )

        assert response.status_code == 200
        organization.refresh_from_db()
        assert organization.parent_id is None

    def test_reparenting_to_a_non_descendant_is_allowed(self, admin_client):
        other_root = baker.make(Organization, name="Other Root", parent=None)
        organization = baker.make(Organization, name="Movable Org", parent=None, slug="movable-org")

        change_url = reverse("admin:organizations_organization_change", args=[organization.pk])
        response = admin_client.post(
            change_url,
            data={
                "name": organization.name,
                "slug": organization.slug,
                "parent": other_root.pk,
                "should_sync_rooms": "",
                "external_event_update_policy": "change_request",
                "week_start": "monday",
            },
        )

        assert response.status_code == 302
        organization.refresh_from_db()
        assert organization.parent_id == other_root.pk


@pytest.mark.django_db
class TestOrganizationAdminSlugValidation:
    """The admin form runs the same shared slug rules as the REST serializer.

    Not the "does the rule reject correctly" job — that is
    ``test_slug_validation.py``'s table-driven job — but that the admin
    surface actually calls into the shared module rather than skipping it.
    """

    def test_setting_a_valid_slug_succeeds(self, admin_client):
        organization = baker.make(Organization, name="Slug Admin Org", parent=None)

        change_url = reverse("admin:organizations_organization_change", args=[organization.pk])
        response = admin_client.post(
            change_url,
            data={
                "name": organization.name,
                "slug": "slug-admin-org",
                "should_sync_rooms": "",
                "external_event_update_policy": "change_request",
                "week_start": "monday",
            },
        )

        assert response.status_code == 302
        organization.refresh_from_db()
        assert organization.slug == "slug-admin-org"

    def test_reserved_word_slug_is_rejected(self, admin_client):
        organization = baker.make(
            Organization, name="Reserved Org", parent=None, slug="reserved-org"
        )
        before = organization.slug

        change_url = reverse("admin:organizations_organization_change", args=[organization.pk])
        response = admin_client.post(
            change_url,
            data={
                "name": organization.name,
                "slug": "admin",
                "should_sync_rooms": "",
                "external_event_update_policy": "change_request",
                "week_start": "monday",
            },
        )

        assert response.status_code == 200
        organization.refresh_from_db()
        assert organization.slug == before

    def test_malformed_slug_is_rejected(self, admin_client):
        organization = baker.make(
            Organization, name="Malformed Org", parent=None, slug="malformed-org"
        )
        before = organization.slug

        change_url = reverse("admin:organizations_organization_change", args=[organization.pk])
        response = admin_client.post(
            change_url,
            data={
                "name": organization.name,
                "slug": "Not_Valid",
                "should_sync_rooms": "",
                "external_event_update_policy": "change_request",
                "week_start": "monday",
            },
        )

        assert response.status_code == 200
        organization.refresh_from_db()
        assert organization.slug == before

    def test_duplicate_slug_is_rejected(self, admin_client):
        baker.make(Organization, name="Existing Org", parent=None, slug="taken-slug")
        organization = baker.make(Organization, name="Second Org", parent=None, slug="second-org")
        before = organization.slug

        change_url = reverse("admin:organizations_organization_change", args=[organization.pk])
        response = admin_client.post(
            change_url,
            data={
                "name": organization.name,
                "slug": "taken-slug",
                "should_sync_rooms": "",
                "external_event_update_policy": "change_request",
                "week_start": "monday",
            },
        )

        assert response.status_code == 200
        organization.refresh_from_db()
        assert organization.slug == before

    def test_confusable_slug_is_rejected_with_confusable_message(self, admin_client):
        """A mixed-script lookalike slug is rejected by the confusables rule, not
        Django's generic ASCII-only SlugField regex message. This exercises the
        actual admin surface: ``OrganizationAdminForm.slug`` must not be left as
        the auto-built ``forms.SlugField`` (whose RegexValidator would preempt
        ``clean_slug`` and raise the generic message before the
        confusable-specific one is reached).
        """
        organization = baker.make(
            Organization, name="Confusable Org", parent=None, slug="confusable-org"
        )
        before = organization.slug

        change_url = reverse("admin:organizations_organization_change", args=[organization.pk])
        # Built from chr() rather than typed as a literal character so the
        # ambiguous codepoint doesn't trip ruff's homoglyph lint (RUF001) on
        # this file (see test_slug_validation.py's TestConfusableSlugs).
        cyrillic_a = chr(0x0430)  # CYRILLIC SMALL LETTER A — visually "a"
        lookalike_slug = cyrillic_a + "cme"
        response = admin_client.post(
            change_url,
            data={
                "name": organization.name,
                "slug": lookalike_slug,
                "should_sync_rooms": "",
                "external_event_update_policy": "change_request",
                "week_start": "monday",
            },
        )

        assert response.status_code == 200
        form = response.context["adminform"].form
        assert "slug" in form.errors
        message = form.errors["slug"][0]
        assert "non-ASCII character" in message
        assert "lookalike" in message
        organization.refresh_from_db()
        assert organization.slug == before

    def test_super_route_slug_is_rejected_as_reserved(self, admin_client):
        """The real admin path segment ``super`` (see ``vinta_schedule_api/urls.py``)
        is rejected as reserved through the admin form.
        """
        organization = baker.make(Organization, name="Super Org", parent=None, slug="super-org")
        before = organization.slug

        change_url = reverse("admin:organizations_organization_change", args=[organization.pk])
        response = admin_client.post(
            change_url,
            data={
                "name": organization.name,
                "slug": "super",
                "should_sync_rooms": "",
                "external_event_update_policy": "change_request",
                "week_start": "monday",
            },
        )

        assert response.status_code == 200
        organization.refresh_from_db()
        assert organization.slug == before

    def test_blank_slug_is_refused_on_an_existing_organization(self, admin_client):
        """Clearing the slug is not a supported operation.

        ``slug`` is NOT NULL and ``Organization.save()`` mints a replacement for
        a blank one, so accepting the blank submission would silently swap the
        organization's public identifier for a different one -- orphaning every
        branded login URL already issued -- rather than removing it.
        """
        organization = baker.make(Organization, name="Blank Slug Org", parent=None, slug="was-set")

        change_url = reverse("admin:organizations_organization_change", args=[organization.pk])
        response = admin_client.post(
            change_url,
            data={
                "name": organization.name,
                "slug": "",
                "should_sync_rooms": "",
                "external_event_update_policy": "change_request",
                "week_start": "monday",
            },
        )

        assert response.status_code == 200
        form = response.context["adminform"].form
        assert "cannot be cleared" in form.errors["slug"][0]
        organization.refresh_from_db()
        assert organization.slug == "was-set"

    def test_adding_an_organization_with_a_blank_slug_mints_an_opaque_one(self, admin_client):
        """Creation is the one place a blank slug is allowed -- and what it
        produces is ``org-<token>``, not ``slugify(name)``: an operator creating
        an organization on someone else's behalf has not consented to publishing
        its name on a public URL."""
        add_url = reverse("admin:organizations_organization_add")

        response = admin_client.post(
            add_url,
            data={
                "name": "Opaque Slug Org",
                "slug": "",
                "should_sync_rooms": "",
                "external_event_update_policy": "change_request",
                "week_start": "monday",
            },
        )

        assert response.status_code == 302
        organization = Organization.objects.get(name="Opaque Slug Org")
        assert organization.slug.startswith("org-")
        assert "opaque" not in organization.slug


@pytest.mark.django_db
class TestOrganizationBrandingAdminParentGuard:
    """``OrganizationBrandingAdmin`` refuses to save branding for an
    organization that has a parent -- admin is not an escape hatch for the
    write gate's permanent refusal (Organization Auth-Area Branding plan,
    Phase 3). Only the parent condition is enforced in admin; entitlement and
    slug are self-serve/billing states an operator may legitimately need to
    seed branding ahead of."""

    def test_saving_branding_for_a_parented_organization_fails_validation(self, admin_client):
        parent = baker.make(Organization, name="Branding Parent", parent=None)
        child = baker.make(Organization, name="Branding Child", parent=parent)

        add_url = reverse("admin:organizations_organizationbranding_add")
        response = admin_client.post(
            add_url,
            data={
                "organization": child.pk,
                "app_name": "ShouldNotSave",
                # Left empty deliberately: the S3Direct widget's re-render on a
                # validation-error response cannot handle a bound non-empty
                # string value (a pre-existing quirk of
                # s3direct_overrides.form_widgets.S3DirectWidget.render,
                # unrelated to this phase) -- an empty value renders fine.
                "logo": "",
                "primary_color": "",
                "secondary_color": "",
                "support_email": "",
                "redirect_url": "",
            },
        )

        # A validation error re-renders the form (200), it does not redirect.
        assert response.status_code == 200
        form = response.context["adminform"].form
        assert "organization" in form.errors
        assert "parent" in form.errors["organization"][0].lower()
        assert not OrganizationBranding.objects.filter(organization=child).exists()

    def test_saving_branding_for_a_parentless_organization_succeeds(self, admin_client):
        organization = baker.make(Organization, name="Branding Root", parent=None)

        add_url = reverse("admin:organizations_organizationbranding_add")
        response = admin_client.post(
            add_url,
            data={
                "organization": organization.pk,
                "app_name": "AllowedApp",
                "logo": "uploads/branding_logos/admin-test-logo.png",
                "primary_color": "",
                "secondary_color": "",
                "support_email": "",
                "redirect_url": "",
            },
        )

        assert response.status_code == 302
        branding = OrganizationBranding.objects.get(organization=organization)
        assert branding.app_name == "AllowedApp"

    def test_editing_existing_branding_for_a_parented_organization_still_fails(self, admin_client):
        """A branding row that predates a reparenting (spec: "A standalone
        organization later gains a parent") cannot be re-saved through admin
        either -- the guard runs on every save, not only on create."""
        parent = baker.make(Organization, name="Later Parent", parent=None)
        organization = baker.make(Organization, name="Later Child", parent=None)
        branding = baker.make(
            OrganizationBranding,
            organization=organization,
            app_name="Original",
        )
        organization.parent = parent
        organization.save(update_fields=["parent"])

        change_url = reverse("admin:organizations_organizationbranding_change", args=[branding.pk])
        response = admin_client.post(
            change_url,
            data={
                "organization": organization.pk,
                "app_name": "Renamed",
                # See the comment on the create-path test above: empty avoids
                # the S3Direct widget's re-render crash on a bound value.
                "logo": "",
                "primary_color": "",
                "secondary_color": "",
                "support_email": "",
                "redirect_url": "",
            },
        )

        assert response.status_code == 200
        branding.refresh_from_db()
        assert branding.app_name == "Original"
