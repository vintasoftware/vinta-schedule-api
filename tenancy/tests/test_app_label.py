"""Phase 1a of the vinta-django-orgs migration: the app rename to ``tenancy``.

Asserts the two halves of the rename's contract independently: every model in
this app answers to the new ``tenancy`` app label (so migrations, permissions,
and content types resolve under the new name), while every concrete model's
table stays at its pre-rename ``organizations_*`` name (so no table is
renamed and no data moves). See the plan's Guiding Decisions,
"App-label collision" row, and ``ai-plans/TRACKING_VINTA_DJANGO_ORGS_MIGRATION.md``.
"""

from django.core.management import call_command

import pytest

from tenancy.models import (
    Organization,
    OrganizationBranding,
    OrganizationInvitation,
    OrganizationMembership,
)


class TestAppLabel:
    def test_organization_app_label_is_tenancy(self):
        assert Organization._meta.app_label == "tenancy"

    def test_organization_membership_app_label_is_tenancy(self):
        assert OrganizationMembership._meta.app_label == "tenancy"

    def test_organization_invitation_app_label_is_tenancy(self):
        assert OrganizationInvitation._meta.app_label == "tenancy"

    def test_organization_branding_app_label_is_tenancy(self):
        assert OrganizationBranding._meta.app_label == "tenancy"


class TestDbTablesUnchanged:
    """No table is renamed along with the app -- every db_table is pinned."""

    def test_organization_db_table(self):
        assert Organization._meta.db_table == "organizations_organization"

    def test_organization_membership_db_table(self):
        assert OrganizationMembership._meta.db_table == "organizations_organizationmembership"

    def test_organization_invitation_db_table(self):
        assert OrganizationInvitation._meta.db_table == "organizations_organizationinvitation"

    def test_organization_branding_db_table(self):
        assert OrganizationBranding._meta.db_table == "organizations_organizationbranding"


class TestNoPendingMigrations:
    """The rename must not leave the autodetector with pending model changes."""

    @pytest.mark.django_db
    def test_makemigrations_check_reports_no_pending_changes(self):
        # Scoped to "tenancy" rather than a bare project-wide check: an
        # unscoped call inspects every app's model state, which makes this
        # test order-dependent on whatever other tests mutate app/model state
        # in the same pytest-xdist worker process (observed flaky under
        # ``pytest -n auto``, stable in isolation). Scoping to the app this
        # phase actually renamed keeps the assertion meaningful without
        # depending on the rest of the suite's isolation guarantees.
        try:
            call_command("makemigrations", "tenancy", "--check", "--dry-run", verbosity=0)
        except SystemExit as exc:
            assert exc.code in (0, None), "makemigrations detected pending model changes"
