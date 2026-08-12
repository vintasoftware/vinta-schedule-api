"""vinta-django-orgs' Django app is installed, and points at *our* two models.

Phase 1c of the vinta-django-orgs migration installs
``organizations.apps.OrganizationsConfig`` alongside our ``tenancy`` app. Two
collisions have to be resolved for that to be safe, and neither of them fails
loudly if it is resolved wrongly at some later date:

* **Model collision.** The package ships concrete ``Organization`` and
  ``OrganizationMembership`` models. Ours pin their ``db_table`` to
  ``organizations_organization`` / ``organizations_organizationmembership`` --
  the names the package's own models would take. Pointing ``ORGANIZATION_MODEL``
  / ``ORGANIZATION_MEMBERSHIP_MODEL`` at ours marks the package's *swapped*, so
  no table is created for them and no phantom CASCADE relation hangs off
  ``User.delete()``. Get it wrong and you get ``models.E028`` -- or, worse, two
  live models over one table.
* **Admin collision.** ``organizations/admin.py`` registers whatever those two
  settings resolve to, which is ours, and ``admin.site.register`` refuses a model
  it already knows. Resolved by ``tenancy/admin.py`` unregistering the package's
  registration before installing its own (see that module's docstring).
"""

from django.apps import apps
from django.conf import settings
from django.contrib import admin
from django.test import override_settings

import organizations.admin as package_admin
import organizations.models as package_models
import pytest

from tenancy.admin import OrganizationAdmin as TenancyOrganizationAdmin
from tenancy.models import Organization, OrganizationMembership


class TestTheAppIsInstalled:
    def test_the_packages_app_is_installed_under_its_own_label(self):
        assert apps.is_installed("organizations")
        assert apps.get_app_config("organizations").name == "organizations"

    def test_our_app_still_answers_to_tenancy(self):
        assert Organization._meta.app_label == "tenancy"
        assert OrganizationMembership._meta.app_label == "tenancy"

    def test_the_package_app_is_not_wired_for_dependency_injection(self):
        """``INTERNAL_INSTALLED_APPS`` drives ``container.wire(packages=...)``
        and must name only this project's apps."""
        assert "organizations" not in settings.INTERNAL_INSTALLED_APPS
        assert "organizations.apps.OrganizationsConfig" not in settings.INTERNAL_INSTALLED_APPS


class TestSwappableModels:
    def test_the_packages_organization_is_swapped_to_ours(self):
        assert package_models.Organization._meta.swapped == "tenancy.Organization"

    def test_the_packages_membership_is_swapped_to_ours(self):
        assert (
            package_models.OrganizationMembership._meta.swapped == "tenancy.OrganizationMembership"
        )

    def test_the_settings_are_top_level_not_nested(self):
        """Django resolves ``Meta.swappable`` with a plain
        ``getattr(settings, name)``, so these two cannot live inside
        ``SHARED_SCHEMA_ORGANIZATIONS`` with the rest of the configuration."""
        assert settings.ORGANIZATION_MODEL == "tenancy.Organization"
        assert settings.ORGANIZATION_MEMBERSHIP_MODEL == "tenancy.OrganizationMembership"
        assert "ORGANIZATION_MODEL" not in settings.SHARED_SCHEMA_ORGANIZATIONS
        assert "ORGANIZATION_MEMBERSHIP_MODEL" not in settings.SHARED_SCHEMA_ORGANIZATIONS

    def test_the_conf_helpers_resolve_to_our_models(self):
        from organizations.conf import get_organization_membership_model, get_organization_model

        assert get_organization_model() is Organization
        assert get_organization_membership_model() is OrganizationMembership

    def test_no_table_is_created_for_the_swapped_out_models(self):
        """A swapped model is excluded from migrations, so nothing ever creates
        its table -- which is what stops it colliding with the pinned
        ``db_table`` on ours."""
        assert package_models.Organization._meta.db_table == Organization._meta.db_table
        assert (
            package_models.OrganizationMembership._meta.db_table
            == OrganizationMembership._meta.db_table
        )
        assert package_models.Organization._meta.managed is True  # not the mechanism
        assert bool(package_models.Organization._meta.swapped) is True  # this is


class TestRetrieverConfiguration:
    def test_only_our_retriever_is_registered(self):
        """The package's ``retrieve_by_domain`` / ``retrieve_by_http_header``
        (``Organization-Slug``) / ``retrieve_by_session`` are plan Non-goals."""
        from organizations.settings import get_setting

        assert get_setting("ORGANIZATION_RETRIEVERS") == [
            "common.org_retrievers.retrieve_by_x_organization_id"
        ]

    def test_there_is_no_default_organization(self):
        from organizations.settings import get_setting

        assert get_setting("DEFAULT_ORGANIZATION_SLUG") is None

    def test_strict_organization_filter_is_still_off(self):
        """Phase 2a turns it on, once the models actually scope implicitly."""
        from organizations.settings import get_setting

        assert get_setting("STRICT_ORGANIZATION_FILTER") is False

    def test_the_package_middleware_is_not_installed(self):
        """Binding stays in ``TenantScopedViewMixin`` (Phase 2b): the package's
        middleware runs before DRF authentication has populated
        ``request.user``, and our resolution rules are membership-aware."""
        assert not any("organizations.middleware" in item for item in settings.MIDDLEWARE)


class TestAdminRegistration:
    def test_our_model_admin_is_registered_for_organization(self):
        assert admin.site.is_registered(Organization)
        assert type(admin.site._registry[Organization]) is TenancyOrganizationAdmin

    def test_the_packages_organization_admin_is_not_registered(self):
        assert not isinstance(admin.site._registry[Organization], package_admin.OrganizationAdmin)

    def test_the_package_still_provides_the_membership_admin(self):
        """``OrganizationMembership`` has no ``ModelAdmin`` of ours, so there is
        nothing to collide with and the package's -- which ``select_related``s
        the user and organization and prefetches groups precisely to keep its
        changelist from N+1-ing on ``__str__`` -- is left in place."""
        assert admin.site.is_registered(OrganizationMembership)
        assert isinstance(
            admin.site._registry[OrganizationMembership],
            package_admin.OrganizationMembershipAdmin,
        )

    @override_settings(SHARED_SCHEMA_ORGANIZATIONS={"ORGANIZATION_RETRIEVERS": []})
    def test_the_registration_survives_override_settings(self):
        """``override_settings`` fires ``setting_changed``, and a settings change
        that touches ``INSTALLED_APPS`` repopulates the app registry and calls
        every ``AppConfig.ready()`` again -- including
        ``django.contrib.admin``'s, which re-runs ``autodiscover()``. The
        resolution in ``tenancy/admin.py`` is a module-level block, so a re-run
        re-imports nothing and cannot unregister our admin a second time.
        """
        assert type(admin.site._registry[Organization]) is TenancyOrganizationAdmin

    def test_repeated_autodiscovery_is_idempotent(self):
        """``autodiscover()`` twice in a row must not raise ``AlreadyRegistered``
        (from the package re-registering) or ``NotRegistered`` (from our
        unregister running against an empty slot).

        This is the re-entrancy that actually matters: a full app-registry
        repopulation (``override_settings(INSTALLED_APPS=...)``) calls every
        ``AppConfig.ready()`` again, and ``django.contrib.admin``'s is
        ``autodiscover()``. It cannot be exercised end-to-end in this project --
        ``public_api.apps.PublicApiConfig.ready()`` calls
        ``register_converter("docs_slug")``, which raises ``ValueError`` on a
        second call, so *any* ``INSTALLED_APPS`` override fails before reaching
        the admin. Pre-existing and unrelated to this phase; recorded here so the
        gap in this test is a known one rather than an assumed absence.
        """
        admin.autodiscover()
        admin.autodiscover()

        assert type(admin.site._registry[Organization]) is TenancyOrganizationAdmin
        assert isinstance(
            admin.site._registry[OrganizationMembership],
            package_admin.OrganizationMembershipAdmin,
        )

    def test_admin_checks_pass(self):
        """``manage.py check --deploy`` runs these; asserted here so an admin
        misconfiguration introduced by the swap fails a test rather than only a
        gate command."""
        assert admin.site.check(None) == []


@pytest.mark.django_db
class TestMembershipGroupsAndPermissionsShipEmpty:
    """The two M2Ms the primary-key unwind made possible.

    They must exist and be readable, and nothing may read them for authorization
    until Phase 3 -- so the only assertion available about their contents is that
    there are none.
    """

    def test_the_fields_exist(self):
        assert OrganizationMembership._meta.get_field("groups").many_to_many
        assert OrganizationMembership._meta.get_field("permissions").many_to_many

    def test_a_new_membership_has_no_groups_and_no_permissions(self):
        from model_bakery import baker

        from users.models import User

        membership = OrganizationMembership.objects.create(
            user=baker.make(User), organization=baker.make(Organization)
        )

        assert list(membership.groups.all()) == []
        assert list(membership.permissions.all()) == []

    def test_no_membership_anywhere_has_a_group(self):
        assert OrganizationMembership.objects.filter(groups__isnull=False).count() == 0
        assert OrganizationMembership.objects.filter(permissions__isnull=False).count() == 0
