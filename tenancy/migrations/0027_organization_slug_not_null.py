"""Phase 1c: ``Organization.slug`` becomes NOT NULL (and a plain CharField).

Split out from ``0025_reparent_onto_package_abstract_bases`` so it lands *after*
``0026_backfill_organization_slugs`` has given every existing row a value --
adding the constraint first would fail on any database with an organization that
never picked a slug, which is most of them.

Two changes in one ``AlterField``, both inherited rather than chosen:

* ``null=True`` -> NOT NULL. ``AbstractOrganization`` declares it that way and the
  plan's "Slug becomes NOT NULL" Guiding Decision keeps the inherited
  declaration instead of overriding it.
* ``SlugField(max_length=63)`` -> ``CharField(max_length=255)``. Also the base's
  declaration. It only widens what the *column* accepts: ``SLUG_MAX_LENGTH`` (63)
  still bounds every write, because every write surface runs
  ``tenancy.slug_validation.validate_organization_slug``, which enforces the
  length itself rather than relying on the column. Dropping ``SlugField`` loses
  nothing either -- its ASCII-only ``RegexValidator`` was already deliberately
  bypassed on both write surfaces (see ``OrganizationAdminForm.slug`` and
  ``OrganizationSerializer.slug``) so the confusables/reserved-word rules could
  produce their own, more specific errors.

Consequence worth naming: "this organization has no public identifier yet" is no
longer a storable state. ``Organization.save()`` derives one for any row created
without it. The ``BrandingWriteGateReason.NO_SLUG`` branch in
``tenancy.permissions`` is left exactly as it was -- this phase changes no
authorization logic -- but it is now reachable only for a slug forced blank
out-of-band.

Reverse restores the nullable ``SlugField(max_length=63)``. It is a genuine undo
of *this* operation (the column widens and narrows losslessly for any value that
passed validation, all of which are <= 63 characters); what it does not undo is
``0026``'s data -- see that migration's own Reverse note.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Organization.slug: NOT NULL, inherited from AbstractOrganization."""

    dependencies = [
        ("tenancy", "0026_backfill_organization_slugs"),
    ]

    operations = [
        migrations.AlterField(
            model_name="organization",
            name="slug",
            field=models.CharField(default=None, max_length=255, unique=True),
            preserve_default=False,
        ),
    ]
