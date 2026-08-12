# Phase 1b of the vinta-django-orgs migration (see ai-plans/2026-08-12-VINTA_
# DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md). Carried forward from the
# Phase 1a review -- see ai-plans/TRACKING_VINTA_DJANGO_ORGS_MIGRATION.md.
#
# `audit/services.py::AuditService.subject_from_instance` persists
# `subject_type=f"{meta.app_label}.{instance.__class__.__name__}"`. Every
# `audit_audit` row written before the tenancy rename (Phase 1a) from what is
# now `tenancy/services.py`, `tenancy/views.py`, and `public_api/mutations.py`
# holds a value like `"organizations.Organization"` /
# `"organizations.OrganizationMembership"`; every row written after the
# rename holds `"tenancy.Organization"` / `"tenancy.OrganizationMembership"`
# instead -- same subject, two on-disk prefixes.
# `AuditRepository.query()` (`audit/repositories.py`) filters on `subject_type`
# by exact match, so a caller who queries `subject_type="tenancy.Organization"`
# (the value every write surface now supplies) silently misses every
# pre-rename row, with no error and no signal that history looks shorter than
# it is.
#
# Idempotent: matches only rows still carrying the old prefix, so re-running
# is a no-op. `subject_type` is a soft reference (no DB FK), so this is a
# plain string rewrite -- no other table is touched.
#
# Confirmed before writing this migration: the real table is `audit_audit`
# (app label `audit`, model name `audit` -- no custom `db_table`), the column
# is `subject_type` (`character varying(255)`, not null). `subject_from_instance`
# is the only call site that builds `subject_type`, and every one of its
# callers (`tenancy/services.py`, `tenancy/views.py`, `public_api/mutations.py`,
# `public_api/services.py`, `payments/services/subscription_service.py`,
# `calendar_integration/services/*.py`, `legal/services.py`) passes a model
# instance whose app label is one of `tenancy`, `public_api`, `payments`,
# `calendar_integration`, or `legal` -- never `organizations`. `organizations`
# was, and is, exclusively this app's pre-rename label, so
# `subject_type LIKE 'organizations.%'` cannot collide with a legitimately
# `organizations.`-prefixed value from any other app.
from __future__ import annotations

from django.db import migrations
from django.db.models import Value
from django.db.models.functions import Replace


_OLD_PREFIX = "organizations."
_NEW_PREFIX = "tenancy."


def backfill_subject_type_namespace_forward(apps, schema_editor) -> None:
    Audit = apps.get_model("audit", "Audit")
    Audit.objects.filter(subject_type__startswith=_OLD_PREFIX).update(
        subject_type=Replace("subject_type", Value(_OLD_PREFIX), Value(_NEW_PREFIX))
    )


def backfill_subject_type_namespace_backward(apps, schema_editor) -> None:
    Audit = apps.get_model("audit", "Audit")
    Audit.objects.filter(subject_type__startswith=_NEW_PREFIX).update(
        subject_type=Replace("subject_type", Value(_NEW_PREFIX), Value(_OLD_PREFIX))
    )


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
        ("tenancy", "0023_move_content_types_to_tenancy"),
    ]

    operations = [
        migrations.RunPython(
            backfill_subject_type_namespace_forward,
            backfill_subject_type_namespace_backward,
        ),
    ]
