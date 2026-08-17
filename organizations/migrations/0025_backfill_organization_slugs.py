"""Give every existing organization a slug, so ``0026`` can make the column NOT NULL.

Derivation, per row, in order:

1. ``slugify(name)``, truncated to ``SLUG_MAX_LENGTH`` and validated with the
   shared rules. On collision with an already-taken slug, a numeric
   disambiguator is appended (``acme-inc``, then ``acme-inc-2``, ...).
2. ``org-<pk>`` (itself disambiguated) when step 1 produces nothing usable --
   an empty slugification (a name in a script ``slugify`` strips entirely), a
   reserved word (``"Admin"`` slugifies to ``admin``), or a value that fails the
   confusable/format rules.

**This is the one place a name-derived slug is written without a human asking
for it.** The slug is public -- it appears in branded login URLs -- so deriving
it from the name publishes the name. Accepted here, and only here, because the
rows this touches predate the slug being mandatory and there is no production
tenant to disclose anything about -- that disclosure trade-off is scoped to
exactly this one-time backfill, made once when ``slug`` became NOT NULL.
``Organization.save()``'s runtime fallback deliberately mints the
opaque ``org-<token>`` form instead.

Idempotent: only rows with ``slug IS NULL`` or ``slug = ''`` are touched, so a
re-run after a partial failure resumes where it stopped and a re-run after a
complete one is a no-op. Batched and ordered by primary key for the same reason.

Coupling note: this imports ``organizations.slug_validation`` and
``organizations.slug_generation`` -- live modules, not historical ones. They are
pure functions over strings with no model access, but the coupling is real: a
future change to the reserved-word list would retroactively change which rows
this migration would send down branch 2 if it were ever re-run from scratch.
Accepted for the same reason ``payments/migrations/0009`` accepts its own
documented exception -- re-deriving 200 lines of validation rules inside a
migration is the larger hazard.

Reverse: a no-op. There is no way to tell a backfilled slug from one an
organization chose, and clearing every slug would fail ``0026``'s NOT NULL on
the way back up.
"""

from django.db import migrations, models

from organizations.slug_generation import disambiguate_slug, name_derived_slug_base


#: Rows per query. Small enough that a failure loses little work, large enough
#: that the backfill of a realistic table is a handful of round trips.
BATCH_SIZE = 500


def backfill_slugs(apps, schema_editor):
    Organization = apps.get_model("organizations", "Organization")
    db_alias = schema_editor.connection.alias
    manager = Organization.objects.using(db_alias)

    # Held in memory rather than re-queried per candidate: the disambiguation
    # loop asks "is this taken?" repeatedly, and rows this run has *assigned*
    # but not yet flushed have to count as taken too.
    taken: set[str] = set(
        manager.exclude(slug__isnull=True).exclude(slug="").values_list("slug", flat=True)
    )

    last_pk = 0
    while True:
        batch = list(
            manager.filter(pk__gt=last_pk)
            .filter(models.Q(slug__isnull=True) | models.Q(slug=""))
            .order_by("pk")[:BATCH_SIZE]
        )
        if not batch:
            break

        for organization in batch:
            slug = _derive(organization, taken)
            organization.slug = slug
            taken.add(slug)

        # ``bulk_update`` rather than ``save()``: the historical model has no
        # ``save()`` override to invoke (that is the point of a historical
        # model), so there is nothing to lose and one round trip to gain.
        manager.bulk_update(batch, ["slug"])
        last_pk = batch[-1].pk


def _derive(organization, taken: set[str]) -> str:
    slug_exists = taken.__contains__

    base = name_derived_slug_base(organization.name or "")
    if base is not None:
        derived = disambiguate_slug(base, slug_exists=slug_exists)
        if derived is not None:
            return derived

    fallback = disambiguate_slug(f"org-{organization.pk}", slug_exists=slug_exists)
    if fallback is None:  # pragma: no cover -- needs 1000 taken variants of one pk
        raise RuntimeError(
            f"Could not derive a free slug for organization {organization.pk}."
        )
    return fallback


def noop_reverse(apps, schema_editor):
    """Deliberately does nothing -- see the module docstring."""


class Migration(migrations.Migration):

    dependencies = [
        ("organizations", "0024_adopt_vinta_orgs_abstract_bases"),
    ]

    operations = [
        migrations.RunPython(backfill_slugs, noop_reverse),
    ]
