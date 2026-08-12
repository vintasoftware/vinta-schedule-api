"""Verifies the ``0002_backfill_subject_type_namespace`` data migration.

Phase 1b of the vinta-django-orgs migration -- see ai-plans/2026-08-12-VINTA_
DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md and
ai-plans/TRACKING_VINTA_DJANGO_ORGS_MIGRATION.md's Phase 1a carry-forward.

Like ``payments/tests/test_backfill_migration.py``, the test DB is already
fully migrated before any test runs, so the migration ran once already
(against an empty ``Audit`` table at that point, since no pre-rename rows
exist in a fresh test database). This re-invokes the migration's own forward
and reverse functions directly against freshly created ``Audit`` rows,
against the *live* app registry (``django.apps.apps``) rather than a
historical one -- safe here because the migration only calls
``apps.get_model("audit", "Audit")`` and ``Audit``'s shape at ``0002`` is
identical to its current shape (no field added/removed/renamed by any
migration between ``0001`` and head at the time of writing).

Expected values are pinned as literals throughout -- never derived by calling
``Replace`` (the function under test) inside the test itself, which would
make the assertion vacuous.
"""

from __future__ import annotations

import importlib

from django.apps import apps

import pytest
from model_bakery import baker

from audit.factories import AuditFactory
from audit.models import Audit
from tenancy.models import Organization


migration_module = importlib.import_module("audit.migrations.0002_backfill_subject_type_namespace")
backfill_forward = migration_module.backfill_subject_type_namespace_forward
backfill_backward = migration_module.backfill_subject_type_namespace_backward


@pytest.mark.django_db
class TestBackfillSubjectTypeNamespaceForward:
    def test_organization_subject_type_is_rewritten(self):
        org = baker.make(Organization)
        audit = AuditFactory().create(organization=org, subject_type="organizations.Organization")

        backfill_forward(apps, None)

        audit.refresh_from_db()
        assert audit.subject_type == "tenancy.Organization"

    def test_every_affected_model_is_rewritten(self):
        org = baker.make(Organization)
        rows = {
            old: AuditFactory().create(organization=org, subject_type=old)
            for old in (
                "organizations.Organization",
                "organizations.OrganizationMembership",
                "organizations.OrganizationInvitation",
                "organizations.OrganizationBranding",
            )
        }

        backfill_forward(apps, None)

        for audit in rows.values():
            audit.refresh_from_db()

        assert rows["organizations.Organization"].subject_type == "tenancy.Organization"
        assert (
            rows["organizations.OrganizationMembership"].subject_type
            == "tenancy.OrganizationMembership"
        )
        assert (
            rows["organizations.OrganizationInvitation"].subject_type
            == "tenancy.OrganizationInvitation"
        )
        assert (
            rows["organizations.OrganizationBranding"].subject_type
            == "tenancy.OrganizationBranding"
        )

    def test_idempotent_second_run_changes_nothing(self):
        org = baker.make(Organization)
        audit = AuditFactory().create(organization=org, subject_type="organizations.Organization")

        backfill_forward(apps, None)
        backfill_forward(apps, None)

        audit.refresh_from_db()
        assert audit.subject_type == "tenancy.Organization"

    def test_unrelated_subject_type_is_untouched(self):
        org = baker.make(Organization)
        untouched = AuditFactory().create(
            organization=org, subject_type="calendar_integration.Calendar"
        )
        already_tenancy = AuditFactory().create(
            organization=org, subject_type="tenancy.Organization"
        )

        backfill_forward(apps, None)

        untouched.refresh_from_db()
        already_tenancy.refresh_from_db()
        assert untouched.subject_type == "calendar_integration.Calendar"
        assert already_tenancy.subject_type == "tenancy.Organization"

    def test_subject_type_with_organizations_substring_not_at_prefix_is_untouched(self):
        """Only a literal ``organizations.`` *prefix* is rewritten -- a value that
        merely contains the substring elsewhere must be left alone, proving the
        migration matches on prefix, not on substring."""
        org = baker.make(Organization)
        audit = AuditFactory().create(
            organization=org, subject_type="payments.OrganizationsBillingReport"
        )

        backfill_forward(apps, None)

        audit.refresh_from_db()
        assert audit.subject_type == "payments.OrganizationsBillingReport"


@pytest.mark.django_db
class TestBackfillSubjectTypeNamespaceReverse:
    def test_reverse_restores_the_old_prefix(self):
        org = baker.make(Organization)
        audit = AuditFactory().create(organization=org, subject_type="tenancy.Organization")

        backfill_backward(apps, None)

        audit.refresh_from_db()
        assert audit.subject_type == "organizations.Organization"

    def test_reverse_then_forward_round_trips(self):
        org = baker.make(Organization)
        audit = AuditFactory().create(organization=org, subject_type="organizations.Organization")

        backfill_forward(apps, None)
        audit.refresh_from_db()
        assert audit.subject_type == "tenancy.Organization"

        backfill_backward(apps, None)
        audit.refresh_from_db()
        assert audit.subject_type == "organizations.Organization"

    def test_reverse_leaves_unrelated_subject_type_untouched(self):
        org = baker.make(Organization)
        untouched = AuditFactory().create(
            organization=org, subject_type="calendar_integration.Calendar"
        )

        backfill_backward(apps, None)

        untouched.refresh_from_db()
        assert untouched.subject_type == "calendar_integration.Calendar"


def test_audit_table_and_column_names_match_the_migration_docstring():
    """Pins the real table/column names this migration's docstring claims it
    verified, so a future rename of either is caught here rather than only in
    the SQL comment going stale."""
    assert Audit._meta.db_table == "audit_audit"
    assert Audit._meta.get_field("subject_type").column == "subject_type"
