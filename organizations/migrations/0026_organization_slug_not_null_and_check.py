"""Make ``Organization.slug`` NOT NULL, and refuse a blank one in the database.

Runs after ``0025`` has given every existing row a value.

The check constraint is not belt-and-braces on top of NOT NULL -- it closes a
different hole. NOT NULL still admits ``''``, and an empty slug is not a slug:
two blank rows collide on the unique index, and, more importantly, a blank slug
is precisely the state ``organizations.permissions.
evaluate_branding_write_gate``'s retired ``NO_SLUG`` condition used to describe.
Without this constraint that state stays *reachable* -- not through ``save()``,
which fills a blank slug in, but through everything that goes around it:
``queryset.update(slug="")``, raw SQL, a future data migration. Retiring a
product rule on the grounds that its condition can no longer occur means making
that true at the only layer no code path can bypass.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("organizations", "0025_backfill_organization_slugs"),
    ]

    operations = [
        migrations.AlterField(
            model_name="organization",
            name="slug",
            field=models.CharField(max_length=255, unique=True),
        ),
        migrations.AddConstraint(
            model_name="organization",
            constraint=models.CheckConstraint(
                condition=models.Q(("slug", ""), _negated=True),
                name="organization_slug_not_blank",
            ),
        ),
    ]
