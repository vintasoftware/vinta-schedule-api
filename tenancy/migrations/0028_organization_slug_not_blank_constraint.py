"""Phase 1c review fix: close the empty-string loophole in ``Organization.slug``.

``0027_organization_slug_not_null`` made ``slug`` NOT NULL, but NOT NULL alone
does not stop ``""`` from being stored -- it satisfies the constraint, is
admitted by the unique index (at most one row may hold it), but is reachable
through no supported write path (every write surface treats a blank submitted
slug as a refusal, not a clear -- see ``OrganizationSerializer.validate_slug``,
``OrganizationAdminForm.clean_slug``, and ``update_branding``'s slug handling).
Before this migration, only convention (``Organization.save()``'s
``if not self.slug`` derivation) kept that state out; this constraint makes it
impossible at the database level, which is what retires "a slug is a
precondition for branding writes" as a live product rule rather than merely an
untested one -- see the plan's Guiding Decisions and
``tenancy.permissions.BrandingWriteGateReason.NO_SLUG``'s docstring for the
authorization-code consequence.

Lock / downtime audit: ``organizations_organization`` is the tenant root, not
a hot table by the add-migration skill's definition (organizations are
created rarely, unlike calendar events or memberships) -- a plain
``AddConstraint`` (rather than ``NOT VALID`` + ``VALIDATE CONSTRAINT``) is
fine here. The validation scan this performs is trivial regardless: every
existing row already satisfies the condition, both because ``slug`` has been
NOT NULL since ``0027`` and because nothing in this codebase has ever written
``""`` to it before this migration existed to forbid it.

Reverse: ``RemoveConstraint`` (Django's default reverse for ``AddConstraint``)
-- a clean, lossless undo; the column's own NOT NULL constraint is untouched.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add ``organization_slug_not_blank`` CHECK constraint."""

    dependencies = [
        ("tenancy", "0027_organization_slug_not_null"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="organization",
            constraint=models.CheckConstraint(
                condition=~models.Q(slug=""),
                name="organization_slug_not_blank",
            ),
        ),
    ]
