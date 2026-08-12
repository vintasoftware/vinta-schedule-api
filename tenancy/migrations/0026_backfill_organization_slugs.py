"""Phase 1c: give every existing organization a slug, before slug becomes NOT NULL.

``Organization.slug`` was nullable ("the organization has not picked a public
identifier yet"). ``AbstractOrganization`` declares it NOT NULL and unique, and
this repo inherits that rather than overriding it (see the plan's "Slug becomes
NOT NULL" Guiding Decision), so every ``NULL`` has to become a real, valid,
unique slug before ``0027_organization_slug_not_null`` can add the constraint.

Algorithm
---------
Delegated wholesale to ``tenancy.slug_generation.derive_organization_slug``,
which is also what ``Organization.save()`` calls for a row created without one --
one implementation, so a backfilled organization and a newly created one cannot
end up under different rules. Summarised (the module has the full contract):

1. ``slugify(name)``, truncated to ``SLUG_MAX_LENGTH``.
2. Validated with ``tenancy.slug_validation.validate_organization_slug`` -- the
   *real* module every write surface uses, not a copy of its rules. A base that
   fails it (reserved word, too short, purely numeric, a character ``slugify``
   keeps but the format rule does not) is skipped.
3. On collision, ``-2``, ``-3``, ... with the base re-truncated to make room and
   each variant re-validated.
4. If nothing derivable is valid and free, ``org-<pk>`` -- unique by
   construction, and passed explicitly here as ``fallback_token`` so the result
   is deterministic (``Organization.save()``, which has no pk yet at that point,
   uses a random token instead).

Failure modes, all landing on step 4 rather than raising: an empty name, a name
that is entirely non-ASCII (``slugify`` strips it to ``""``), a name that
slugifies to a reserved route word (``"Admin"`` -> ``"admin"``), a name shorter
than three characters, a purely numeric name (``"2024"``), and a name whose
slugified form keeps a character the format rule rejects (``"Foo_Bar"`` ->
``"foo_bar"``).

Idempotency
-----------
The queryset selects only ``slug IS NULL`` or ``slug = ''``, so a second run
sees nothing. Rows are processed in ``pk`` order and the taken-slug set is
maintained in memory as it goes, so two organizations with the same name in the
same run get ``acme-inc`` and ``acme-inc-2`` rather than colliding.

Importing from ``tenancy.slug_generation`` / ``tenancy.slug_validation``
------------------------------------------------------------------------
Both are pure functions over strings -- no model imports, no manager access, no
settings beyond two integer constants -- so the usual reason to re-derive a data
migration's logic against ``apps.get_model`` historical state (that a live module
may later change what a historical migration does) is bounded here to the slug
*rules*. If those rules ever tighten, re-running this migration on an
already-backfilled database is still a no-op (nothing is ``NULL``), and the
values it wrote stay whatever they were. Precedent for a deliberate live import
from a data migration: ``payments/migrations/0009_backfill_unlimited_subscriptions``.

Reverse
-------
``RunPython.noop``, deliberately. Once written, a derived slug is
indistinguishable from one an admin typed -- there is no marker to key an exact
undo on, and nulling every slug that *looks* derived would discard hand-picked
ones. Reversing ``0027`` restores the column's nullability, which is the part of
the change that is actually reversible; the values stay, and they are valid
slugs. Consistent with the plan's "Pre-launch posture" (no per-phase reverse path
is guaranteed) and its Rollback note (Phase 1c onward is not cleanly revertible).
"""

from django.db import migrations
from django.db.models import Q

from tenancy.slug_generation import derive_organization_slug


#: What counts as "has no slug". ``slug = ''`` cannot exist today (the field's
#: ``default=None`` and every write surface normalise blank to NULL), but it
#: would break the NOT NULL + unique pair just as badly as a NULL would, and
#: covering it costs nothing.
UNSET_SLUG = Q(slug__isnull=True) | Q(slug="")


def backfill_slugs(apps, schema_editor):
    """Fill every NULL/blank ``Organization.slug`` with a derived, unique value."""
    Organization = apps.get_model("tenancy", "Organization")

    taken = set(
        Organization.objects.exclude(UNSET_SLUG).values_list("slug", flat=True)
    )

    for organization in Organization.objects.filter(UNSET_SLUG).order_by("pk").iterator():
        slug = derive_organization_slug(
            organization.name,
            slug_exists=taken.__contains__,
            fallback_token=str(organization.pk),
        )
        taken.add(slug)
        Organization.objects.filter(pk=organization.pk).update(slug=slug)


class Migration(migrations.Migration):
    """Backfill Organization.slug ahead of the NOT NULL constraint."""

    dependencies = [
        ("tenancy", "0025_reparent_onto_package_abstract_bases"),
    ]

    operations = [
        migrations.RunPython(backfill_slugs, migrations.RunPython.noop),
    ]
