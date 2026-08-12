"""Regression gate: our app is ``organizations``, and every table is where it was.

This module exists because of a decision that was *reversed*. The plan's first
draft targeted ``vinta-django-orgs`` ``0.1.1``, whose own Django app was also
labelled ``organizations``. To resolve that collision it renamed **our** app to
``tenancy`` -- which in turn required ``db_table`` pins on every model, a rewrite
of 79 migration files, a ``django_content_type`` / ``auth_permission`` relabel
migration, an ``audit.subject_type`` namespace backfill (audit rows persist the
app label as a string, so the rename silently split audit history in two), and a
management command to repair ``django_migrations`` on any database seeded before
the branch.

``0.2.0`` renames the *package's* app labels to ``vinta_orgs`` /
``vinta_orgs_custom_data``, so there is no collision and none of that is needed.
All of it was withdrawn. The assertions below are what stop it being
accidentally re-earned: a change that renames our app, pins a ``db_table``, or
installs the package under the wrong label fails here, loudly, in one place --
rather than surfacing months later as an audit trail that looks shorter than it
is.
"""

from django.apps import apps
from django.core.management import call_command

import pytest

from organizations.models import (
    Organization,
    OrganizationBranding,
    OrganizationInvitation,
    OrganizationMembership,
)


#: Every model this app owns, with the table name it has had since its
#: ``0001_initial`` -- which is exactly what Django's ``{app_label}_{model_name}``
#: default derives while the app label stays ``organizations``. Spelled out as
#: literals rather than derived from ``_meta``, which would make the assertion
#: self-comparing.
EXPECTED_TABLES = {
    Organization: "organizations_organization",
    OrganizationMembership: "organizations_organizationmembership",
    OrganizationInvitation: "organizations_organizationinvitation",
    OrganizationBranding: "organizations_organizationbranding",
}


class TestOurAppIdentity:
    def test_the_app_label_is_organizations(self):
        for model in EXPECTED_TABLES:
            assert model._meta.app_label == "organizations"

    def test_every_table_keeps_its_original_name(self):
        for model, expected_table in EXPECTED_TABLES.items():
            assert model._meta.db_table == expected_table

    def test_no_model_pins_db_table(self):
        """The names above must come from Django's default, not from a pin.

        A pin would keep the table names right while letting the app label
        drift, which is the half-migration the withdrawn rename produced.
        ``Meta.original_attrs`` holds exactly the options the model's ``Meta``
        declared, so an absent ``db_table`` key proves the name is derived.
        """
        for model in EXPECTED_TABLES:
            assert "db_table" not in model._meta.original_attrs

    def test_the_app_config_is_reachable_under_the_organizations_label(self):
        app_config = apps.get_app_config("organizations")
        assert app_config.name == "organizations"


class TestThePackageIsInstalledUnderItsOwnLabel:
    def test_the_package_app_is_installed_as_vinta_orgs(self):
        app_config = apps.get_app_config("vinta_orgs")
        assert app_config.name == "vinta_orgs"

    def test_the_packages_own_models_are_swapped_out(self):
        """``ORGANIZATION_MODEL`` / ``ORGANIZATION_MEMBERSHIP_MODEL`` point at ours.

        Without this the package's concrete models would get their own tables
        and their own CASCADE from ``User``, and every relation the package
        declares would point at rows nothing in this project writes.
        """
        from vinta_orgs.conf import get_organization_membership_model, get_organization_model

        assert get_organization_model() is Organization
        assert get_organization_membership_model() is OrganizationMembership

        package_organization = apps.get_model("vinta_orgs", "Organization")
        package_membership = apps.get_model("vinta_orgs", "OrganizationMembership")
        assert package_organization._meta.swapped == "organizations.Organization"
        assert package_membership._meta.swapped == "organizations.OrganizationMembership"

    def test_the_custom_data_app_is_not_installed(self):
        """``vinta_orgs_custom_data`` is a Non-goal -- per-organization dynamic
        tables are not something this project has, or wants, tables for."""
        assert not apps.is_installed("vinta_orgs_custom_data")


class TestNoRenameArtifactsSurvive:
    def test_no_app_is_labelled_tenancy(self):
        """The withdrawn rename's target label. If this ever resolves, the
        rename has come back -- along with every consequence named in this
        module's docstring."""
        assert not apps.is_installed("tenancy")

    @pytest.mark.django_db
    def test_no_migration_history_row_names_a_tenancy_app(self):
        from django.db.migrations.recorder import MigrationRecorder

        assert not MigrationRecorder.Migration.objects.filter(app="tenancy").exists()


@pytest.mark.django_db
def test_makemigrations_reports_nothing_pending_for_this_app():
    """Scoped to ``organizations`` deliberately.

    A project-wide ``makemigrations --check`` here is flaky under ``pytest -n
    auto``: another test in the same worker process can leave live app/model
    state mutated, and the autodetector then reports a diff that has nothing to
    do with this app. The repository-wide check is the outer gate's job.
    """
    call_command("makemigrations", "organizations", "--check", "--dry-run", verbosity=0)
