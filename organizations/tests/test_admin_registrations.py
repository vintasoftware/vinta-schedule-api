"""The package's ``ModelAdmin``s must not be the ones registered.

``vinta_orgs.admin`` registers against whichever models ``ORGANIZATION_MODEL``
and ``ORGANIZATION_MEMBERSHIP_MODEL`` name -- which, since this project swapped
both, means *our* models. ``organizations/admin.py`` unregisters both before
registering its own.

This is an authorization surface, not cosmetics. The package's
``OrganizationMembershipAdmin`` exposes ``groups`` and ``permissions`` as plain,
staff-editable fields, with none of the rules the REST viewset enforces: the
seat limit, and the refusal to demote the last member who can manage members.
Those two relations are the *entire* authorization surface of a membership, so
left registered, any staff user holding the change permission could grant
themselves organization admin or billing management in one form post, and none
of the audit or webhook side effects the service layer emits would fire.
"""

from django.contrib import admin

import pytest

from organizations.admin import OrganizationAdmin
from organizations.models import Organization, OrganizationBranding, OrganizationMembership


class TestMembershipHasNoAdminSurface:
    def test_organization_membership_is_not_registered_at_all(self):
        """No membership admin ships yet.

        Unregistering the package's without registering a replacement is the
        point: a membership admin that carries the seat-limit and
        last-admin-protection rules does not exist yet, and until it does
        "no surface" is safer than "an unguarded one".
        """
        assert not admin.site.is_registered(OrganizationMembership)

    def test_the_packages_membership_admin_is_not_registered_against_any_model(self):
        from vinta_orgs.admin import OrganizationMembershipAdmin

        registered = {type(model_admin) for model_admin in admin.site._registry.values()}
        assert OrganizationMembershipAdmin not in registered

    def test_no_registered_admin_exposes_the_capability_relations(self):
        """Belt and braces, phrased as the property that actually matters.

        If a membership admin is ever registered -- here or later -- it must not
        put the two relations that carry every capability into a form with no
        rules attached.
        """
        forbidden = {"groups", "permissions"}

        for model, model_admin in admin.site._registry.items():
            if model is not OrganizationMembership:
                continue
            exposed = set(model_admin.get_fields(request=None) or ())
            assert not (exposed & forbidden), (
                f"{type(model_admin).__name__} exposes {exposed & forbidden} as editable "
                "membership fields with none of the REST viewset's seat-limit or "
                "last-admin-protection rules."
            )


class TestOrganizationAdminIsOurs:
    def test_the_registered_organization_admin_is_the_one_in_this_app(self):
        assert admin.site.is_registered(Organization)
        assert isinstance(admin.site._registry[Organization], OrganizationAdmin)

    def test_the_packages_organization_admin_is_not_registered(self):
        from vinta_orgs.admin import OrganizationAdmin as PackageOrganizationAdmin

        registered = {type(model_admin) for model_admin in admin.site._registry.values()}
        assert PackageOrganizationAdmin not in registered

    def test_ours_does_not_inline_organization_site(self):
        """This project does not do domain-based tenancy. The package's admin inlines
        ``OrganizationSite`` with ``min_num=1``, which would make saving an
        organization through the admin require a ``Site`` row."""
        from vinta_orgs.models import OrganizationSite

        inline_models = {inline.model for inline in admin.site._registry[Organization].inlines}
        assert OrganizationSite not in inline_models

    def test_ours_keeps_its_slug_validating_form(self):
        from organizations.admin import OrganizationAdminForm

        assert admin.site._registry[Organization].form is OrganizationAdminForm

    def test_branding_admin_is_still_registered(self):
        """Unregistering by model name is easy to over-apply; this pins that the
        app's other admin was left alone."""
        assert admin.site.is_registered(OrganizationBranding)


@pytest.mark.django_db
class TestTheAdminIndexStillLoads:
    def test_a_superuser_can_load_the_admin_index(self, client):
        """A double registration raises ``AlreadyRegistered`` at import time and
        a stale unregistration leaves ``admin.site`` inconsistent; both surface
        here rather than only in production."""
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        user = get_user_model().objects.create_superuser(
            email="admin-registrations@example.com",
            password="adminpassword",  # noqa: S106
        )
        client.force_login(user)

        response = client.get(reverse("admin:index"))

        assert response.status_code == 200
