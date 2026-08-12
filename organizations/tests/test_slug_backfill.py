"""``Organization.slug``: the backfill, the derivation rules, and the two constraints.

Three layers, deliberately separate:

* the derivation helpers in ``organizations.slug_generation``, tested as pure
  functions (no database, no migration machinery) -- collision disambiguation,
  the reserved-word fallback, and the name/opaque split that is a *disclosure*
  decision rather than a formatting one;
* the ``0025`` data migration itself, driven backwards and forwards against a
  real database so the NULL branch it exists for is actually executed;
* the ``0026`` constraints -- NOT NULL, and ``organization_slug_not_blank``,
  which is what makes the branding gate's retired ``NO_SLUG`` condition
  unreachable rather than merely unlikely.
"""

from __future__ import annotations

import importlib

from django.apps import apps as global_apps
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

import pytest
from model_bakery import baker

from organizations.models import Organization
from organizations.slug_generation import (
    SlugDerivationError,
    derive_organization_slug,
    disambiguate_slug,
    name_derived_slug_base,
    opaque_organization_slug,
)


APP_LABEL = "organizations"
BACKFILL_MIGRATION = "0025_backfill_organization_slugs"
BEFORE_BACKFILL = "0024_adopt_vinta_orgs_abstract_bases"
AFTER_CONSTRAINTS = "0026_organization_slug_not_null_and_check"


def _never_taken(_candidate: str) -> bool:
    return False


class TestNameDerivedSlugBase:
    def test_slugifies_an_ordinary_name(self):
        assert name_derived_slug_base("Acme Inc.") == "acme-inc"

    def test_rejects_a_name_that_slugifies_to_a_reserved_word(self):
        """``"Admin"`` slugifies to ``admin``, which
        ``organizations.slug_validation`` reserves for our own routes. The
        caller is expected to fall back rather than mangle it into compliance --
        a mangled slug no longer resembles the name it was meant to disclose."""
        assert name_derived_slug_base("Admin") is None

    def test_rejects_a_name_that_slugifies_to_nothing(self):
        assert name_derived_slug_base("日本語") is None
        assert name_derived_slug_base("   ") is None

    def test_rejects_a_purely_numeric_name(self):
        """A numeric slug reads as an organization id -- the enumerable
        identifier the slug exists to replace."""
        assert name_derived_slug_base("2026") is None


class TestDisambiguateSlug:
    def test_returns_the_base_when_it_is_free(self):
        assert disambiguate_slug("acme-inc", slug_exists=_never_taken) == "acme-inc"

    def test_appends_a_counter_on_collision(self):
        taken = {"acme-inc"}
        assert disambiguate_slug("acme-inc", slug_exists=taken.__contains__) == "acme-inc-2"

    def test_keeps_counting_past_the_first_collision(self):
        taken = {"acme-inc", "acme-inc-2", "acme-inc-3"}
        assert disambiguate_slug("acme-inc", slug_exists=taken.__contains__) == "acme-inc-4"

    def test_the_numbered_variant_stays_within_the_length_limit(self):
        from organizations.slug_validation import SLUG_MAX_LENGTH

        base = "a" * SLUG_MAX_LENGTH
        taken = {base}

        result = disambiguate_slug(base, slug_exists=taken.__contains__)

        assert result is not None
        assert len(result) <= SLUG_MAX_LENGTH
        assert result.endswith("-2")


class TestOpaqueOrganizationSlug:
    def test_is_prefixed_and_random(self):
        first = opaque_organization_slug(slug_exists=_never_taken)
        second = opaque_organization_slug(slug_exists=_never_taken)

        assert first.startswith("org-")
        assert first != second

    def test_raises_rather_than_looping_forever_when_nothing_is_free(self):
        with pytest.raises(SlugDerivationError):
            opaque_organization_slug(slug_exists=lambda _candidate: True)


class TestDeriveOrganizationSlug:
    def test_does_not_disclose_the_name_by_default(self):
        """The default matters more than the option.

        ``Organization.save()`` calls this with the default, so anything that
        flips it publishes the organization's name in a URL for every row saved
        without an explicit slug.
        """
        slug = derive_organization_slug("Acme Incorporated", slug_exists=_never_taken)

        assert slug.startswith("org-")
        assert "acme" not in slug

    def test_discloses_the_name_when_asked(self):
        slug = derive_organization_slug(
            "Acme Incorporated", slug_exists=_never_taken, disclose_name=True
        )

        assert slug == "acme-incorporated"

    def test_falls_back_to_the_opaque_form_when_the_name_yields_nothing(self):
        slug = derive_organization_slug("Admin", slug_exists=_never_taken, disclose_name=True)

        assert slug.startswith("org-")


@pytest.mark.django_db(transaction=True)
class TestTheBackfillMigration:
    """Drives ``0025`` against a real database, backwards then forwards.

    Necessary rather than incidental: ``slug`` is NOT NULL from ``0026``
    onwards, so the rows the backfill exists to fix cannot be created at all
    while the current schema is in place. Reversing to ``0024`` (nullable,
    unconstrained) is the only way to execute the branch.

    Restores the schema in ``finally``. This manipulates the shared per-worker
    test database directly, following the precedent in
    ``payments/tests/test_billing_period_summary_model.py``.
    """

    def _insert_unslugged(self, rows: list[tuple[str, str | None]]) -> list[int]:
        """Insert ``(name, slug)`` rows straight through SQL, returning their ids."""
        ids = []
        with connection.cursor() as cursor:
            for name, slug in rows:
                cursor.execute(
                    """
                    INSERT INTO organizations_organization
                    (name, slug, should_sync_rooms, external_event_update_policy,
                     week_start, created, modified, can_invite_organizations)
                    VALUES (%s, %s, false, 'change_request', 'monday', NOW(), NOW(), false)
                    RETURNING id
                    """,
                    [name, slug],
                )
                ids.append(cursor.fetchone()[0])
        return ids

    def _slugs_for(self, ids: list[int]) -> list[str]:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, slug FROM organizations_organization WHERE id = ANY(%s)", [ids]
            )
            by_id = dict(cursor.fetchall())
        return [by_id[row_id] for row_id in ids]

    def test_backfill_derivation_collisions_idempotency_and_constraints(self):
        # Bound before the try so the finally can always clean up, even if the
        # inserts themselves are what failed.
        ids: list[int] = []
        executor = MigrationExecutor(connection)
        try:
            executor.migrate([(APP_LABEL, BEFORE_BACKFILL)])
            executor.loader.build_graph()

            ids += self._insert_unslugged(
                [
                    # Ordinary name -> slugify.
                    ("Acme Inc.", None),
                    # Same name again -> numeric disambiguator.
                    ("Acme Inc.", None),
                    # Reserved word -> org-<pk> fallback.
                    ("Admin", None),
                    # Slugifies to nothing -> org-<pk> fallback.
                    ("日本語", None),
                    # Blank rather than NULL -- also unslugged, also backfilled.
                    ("Blank Slug Org", ""),
                    # Already slugged -- must be left exactly as it is.
                    ("Pre Slugged Org", "chosen-by-hand"),
                ]
            )

            executor = MigrationExecutor(connection)
            executor.migrate([(APP_LABEL, AFTER_CONSTRAINTS)])
            executor.loader.build_graph()

            acme, acme_2, reserved, unslugifiable, blank, pre_slugged = self._slugs_for(ids)

            assert acme == "acme-inc"
            assert acme_2 == "acme-inc-2"
            assert reserved == f"org-{ids[2]}"
            assert unslugifiable == f"org-{ids[3]}"
            assert blank == "blank-slug-org"
            assert pre_slugged == "chosen-by-hand"

            # Idempotent: re-running the backfill against the now-complete table
            # changes nothing, because it only ever selects NULL/blank rows.
            backfill = importlib.import_module(f"{APP_LABEL}.migrations.{BACKFILL_MIGRATION}")
            backfill.backfill_slugs(global_apps, connection.schema_editor())
            assert self._slugs_for(ids) == [
                acme,
                acme_2,
                reserved,
                unslugifiable,
                blank,
                pre_slugged,
            ]

            # 0026's two constraints are now in place.
            with pytest.raises(IntegrityError), transaction.atomic():
                self._insert_unslugged([("Post Constraint Null", None)])

            with pytest.raises(IntegrityError), transaction.atomic():
                self._insert_unslugged([("Post Constraint Blank", "")])
        finally:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM organizations_organization WHERE id = ANY(%s)", [ids])
            executor = MigrationExecutor(connection)
            executor.migrate([(APP_LABEL, AFTER_CONSTRAINTS)])
            executor.loader.build_graph()


@pytest.mark.django_db(transaction=True)
class TestTheCheckConstraintOnLiveSchema:
    def test_a_blank_slug_written_past_save_is_rejected(self):
        """``Organization.save()`` fills a blank slug in, so the interesting
        write is the one that goes around it. This is what makes the branding
        gate's ``NO_SLUG`` retirement permanent rather than merely untested."""
        organization = baker.make(Organization, slug="a-real-slug")

        with pytest.raises(IntegrityError), transaction.atomic():
            Organization.objects.filter(pk=organization.pk).update(slug="")

    def test_a_null_slug_written_past_save_is_rejected(self):
        organization = baker.make(Organization, slug="another-real-slug")

        with pytest.raises(IntegrityError), transaction.atomic():
            Organization.objects.filter(pk=organization.pk).update(slug=None)
