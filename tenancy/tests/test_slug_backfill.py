"""``Organization.slug``'s Phase 1c backfill and the NOT NULL constraint after it.

Two things are under test, and they are deliberately tested at different levels:

* ``tenancy.slug_generation.derive_organization_slug`` -- the algorithm shared by
  ``tenancy/migrations/0026_backfill_organization_slugs`` and
  ``Organization.save()``. Tested directly, driving the ``slug_exists``
  predicate, because that is where every branch lives (collision
  disambiguation, reserved-word fallback, idempotent re-derivation) and the
  migration is a thin loop over it.
* the column's shape afterwards -- NOT NULL, unique, and unable to store the
  ``None`` the field used to default to.

Expected slugs are pinned as literals throughout rather than derived from
``slugify`` or from the module under test: a test that computes its expectation
the same way the code does cannot catch the code computing it wrongly.
"""

import importlib
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

import pytest
from model_bakery import baker

from tenancy.models import Organization
from tenancy.slug_generation import derive_organization_slug


migration_module = importlib.import_module("tenancy.migrations.0026_backfill_organization_slugs")


def _taken(*slugs: str):
    """A ``slug_exists`` predicate over a fixed set, with no database."""
    claimed = set(slugs)
    return claimed.__contains__


class TestDeriveOrganizationSlug:
    """The shared algorithm. No database -- the predicate is injected."""

    def test_derives_from_the_name(self):
        assert derive_organization_slug("Acme Inc", slug_exists=_taken()) == "acme-inc"

    def test_lowercases_and_strips_punctuation(self):
        assert derive_organization_slug("O'Brien & Sons, Ltd.", slug_exists=_taken()) == (
            "obrien-sons-ltd"
        )

    def test_disambiguates_a_collision_with_a_numeric_suffix(self):
        assert derive_organization_slug("Acme Inc", slug_exists=_taken("acme-inc")) == "acme-inc-2"

    def test_walks_the_disambiguator_past_several_collisions(self):
        assert (
            derive_organization_slug(
                "Acme Inc", slug_exists=_taken("acme-inc", "acme-inc-2", "acme-inc-3")
            )
            == "acme-inc-4"
        )

    def test_falls_back_to_org_pk_on_a_reserved_word(self):
        """``"Admin"`` slugifies to the reserved route word ``admin``."""
        assert (
            derive_organization_slug("Admin", slug_exists=_taken(), fallback_token="42") == "org-42"
        )

    def test_falls_back_to_org_pk_on_a_vendor_reserved_word(self):
        assert (
            derive_organization_slug("Vinta", slug_exists=_taken(), fallback_token="7") == "org-7"
        )

    def test_falls_back_to_org_pk_on_a_name_that_slugifies_to_nothing(self):
        """A name made entirely of characters ``slugify`` strips."""
        assert (
            derive_organization_slug("!!! ???", slug_exists=_taken(), fallback_token="9") == "org-9"
        )

    def test_falls_back_to_org_pk_on_an_all_non_ascii_name(self):
        assert (
            derive_organization_slug("会社", slug_exists=_taken(), fallback_token="11") == "org-11"
        )

    def test_falls_back_to_org_pk_on_a_too_short_name(self):
        """``SLUG_MIN_LENGTH`` is 3, so ``"AB"`` cannot be a slug."""
        assert derive_organization_slug("AB", slug_exists=_taken(), fallback_token="13") == "org-13"

    def test_falls_back_to_org_pk_on_a_purely_numeric_name(self):
        """A numeric slug reads as an organization id -- the enumerable
        identifier the slug exists to replace."""
        assert (
            derive_organization_slug("2024", slug_exists=_taken(), fallback_token="17") == "org-17"
        )

    def test_falls_back_to_org_pk_when_slugify_keeps_an_underscore(self):
        """``slugify`` keeps ``_`` (it is a word character); the format rule does
        not admit it. This is the failure mode that is invisible unless the real
        validation module is consulted rather than reimplemented."""
        assert (
            derive_organization_slug("Foo_Bar", slug_exists=_taken(), fallback_token="19")
            == "org-19"
        )

    def test_disambiguates_the_fallback_too(self):
        assert (
            derive_organization_slug("Admin", slug_exists=_taken("org-42"), fallback_token="42")
            == "org-42-2"
        )

    def test_truncates_a_long_name_to_the_slug_length_bound(self):
        """``SLUG_MAX_LENGTH`` is 63; the derived slug must fit it and must not
        end on the hyphen the truncation lands in."""
        name = "Aaaa " * 20  # 100 characters, slugifies to 20 "aaaa" groups
        slug = derive_organization_slug(name, slug_exists=_taken(), fallback_token="23")

        assert slug == "aaaa-aaaa-aaaa-aaaa-aaaa-aaaa-aaaa-aaaa-aaaa-aaaa-aaaa-aaaa-aaa"
        assert len(slug) == 63

    def test_a_random_fallback_token_is_used_when_none_is_given(self):
        """``Organization.save()`` has no pk yet, so it passes no token."""
        slug = derive_organization_slug("Admin", slug_exists=_taken())

        assert slug.startswith("org-")
        assert slug != "org-"


@pytest.mark.django_db
class TestBackfillMigrationBehaviour:
    """The migration's loop, driven through the historical model.

    The migration itself has already run against the test database (it is part
    of the graph pytest-django applies), so what is exercised here is the
    function it delegates to, against real rows and the real uniqueness
    predicate -- the same shape ``OrganizationService.create_organization``
    uses (the one write path that still derives a slug from the name --
    ``Organization.save()``'s own default is the opaque, non-name-derived
    form; see both methods' docstrings).
    """

    def _existing_slug_predicate(self):
        return lambda candidate: Organization.objects.filter(slug=candidate).exists()

    def test_two_organizations_with_the_same_name_do_not_collide(self):
        from tenancy.services import OrganizationService

        service = OrganizationService()
        first = service.create_organization(
            creator=baker.make(get_user_model()), name="Duplicate Name Ltd"
        )
        second = service.create_organization(
            creator=baker.make(get_user_model()), name="Duplicate Name Ltd"
        )
        third = service.create_organization(
            creator=baker.make(get_user_model()), name="Duplicate Name Ltd"
        )

        assert first.slug == "duplicate-name-ltd"
        assert second.slug == "duplicate-name-ltd-2"
        assert third.slug == "duplicate-name-ltd-3"

    def test_a_reserved_name_lands_on_the_pk_fallback(self):
        organization = baker.make(Organization, name="Admin", slug="placeholder-slug")

        slug = derive_organization_slug(
            organization.name,
            slug_exists=self._existing_slug_predicate(),
            fallback_token=str(organization.pk),
        )

        assert slug == f"org-{organization.pk}"

    def test_re_running_the_derivation_is_stable_for_an_already_backfilled_row(self):
        """Idempotency, at the level that matters: a second pass finds nothing to
        do, because the migration only selects rows whose slug is NULL or blank
        and every row now has one -- regardless of which write path put it
        there. ``Organization.objects.create(...)`` (no explicit slug) goes
        through ``Organization.save()``'s own opaque-default fallback, not
        name-derivation -- see that method's docstring."""
        Organization.objects.create(name="Stable Org")

        assert Organization.objects.filter(slug__isnull=True).count() == 0
        assert Organization.objects.filter(slug="").count() == 0
        stable_slug = Organization.objects.get(name="Stable Org").slug
        assert stable_slug
        assert stable_slug.startswith("org-")

    def test_the_backfill_migration_is_applied_and_has_a_reverse(self):
        """The migration exists in the graph, is applied, and declares a reverse
        (``RunPython.noop``) so ``migrate tenancy 0025`` is not refused."""
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()

        node = ("tenancy", "0026_backfill_organization_slugs")
        assert node in executor.loader.graph.nodes
        assert node in executor.loader.applied_migrations

        migration = executor.loader.disk_migrations[node]
        (operation,) = migration.operations
        assert operation.reversible


@pytest.fixture
def historical_organization_model():
    """The ``Organization`` model as it existed at ``0025`` -- nullable
    ``slug``, no NOT NULL constraint, no ``organization_slug_not_blank`` CHECK
    constraint -- so a test can construct the ``slug = ''`` state ``0026``'s
    ``UNSET_SLUG`` predicate selects for.

    That state is no longer storable through the live model: ``0027`` made
    the column NOT NULL, and the Phase 1c review's
    ``organization_slug_not_blank`` CHECK constraint (``0028``) closed the
    empty-string loophole NOT NULL alone left open. Driving the executor back
    to ``0025`` (real DDL, not just Django's in-memory state) is the only way
    left to reach this state at all -- see ``TestBackfillMigrationNullBranch``
    above for the same technique applied to the ``slug IS NULL`` half.

    Restores head afterward regardless of test outcome.
    """
    app_label = "tenancy"
    previous = "0025_reparent_onto_package_abstract_bases"

    executor = MigrationExecutor(connection)
    head = executor.loader.graph.leaf_nodes(app=app_label)[0]

    try:
        executor.migrate([(app_label, previous)])
        executor.loader.build_graph()

        historical_state = executor.loader.project_state((app_label, previous))
        yield historical_state.apps.get_model("tenancy", "Organization")
    finally:
        executor.migrate([head])
        executor.loader.build_graph()


@pytest.mark.django_db(transaction=True)
class TestBackfillSlugsFunctionAgainstRealRows:
    """``0026``'s own ``backfill_slugs``, run against a real (if temporarily
    rewound) table -- see ``historical_organization_model`` above for why a
    historical model is what makes ``slug = ''`` reachable at all now.

    **One blank row at a time** -- ``slug`` is unique, so ``''`` admits
    exactly one holder; multi-row disambiguation *within one run* is covered
    at the algorithm level in ``TestDeriveOrganizationSlug``, and
    disambiguation against rows already in the table is covered here, which is
    the half a pure-function test cannot reach.
    """

    def _run(self, historical_organization_model) -> None:
        # `backfill_slugs` only reads `apps.get_model(...)` off whatever it is
        # handed, so passing an object exposing just that method (rather than
        # the full historical `apps` registry) is enough.
        migration_module.backfill_slugs(
            SimpleNamespace(get_model=lambda app_label, model_name: historical_organization_model),
            None,
        )

    def test_a_blank_slug_is_filled_from_the_name(self, historical_organization_model):
        organization = historical_organization_model.objects.create(name="Backfill Me Ltd", slug="")

        self._run(historical_organization_model)

        organization.refresh_from_db()
        assert organization.slug == "backfill-me-ltd"

    def test_it_disambiguates_against_a_slug_already_in_the_table(
        self, historical_organization_model
    ):
        """The real uniqueness predicate, not an injected one: the taken set is
        seeded from the rows that already have slugs."""
        historical_organization_model.objects.create(name="Occupier", slug="collide-me")
        organization = historical_organization_model.objects.create(name="Collide Me", slug="")

        self._run(historical_organization_model)

        organization.refresh_from_db()
        assert organization.slug == "collide-me-2"

    def test_a_reserved_name_falls_back_to_org_pk(self, historical_organization_model):
        organization = historical_organization_model.objects.create(name="Admin", slug="")

        self._run(historical_organization_model)

        organization.refresh_from_db()
        assert organization.slug == f"org-{organization.pk}"

    def test_re_running_it_changes_nothing(self, historical_organization_model):
        organization = historical_organization_model.objects.create(name="Idempotent Org", slug="")

        self._run(historical_organization_model)
        organization.refresh_from_db()
        first_pass = organization.slug
        assert first_pass == "idempotent-org"

        self._run(historical_organization_model)
        organization.refresh_from_db()
        assert organization.slug == first_pass

    def test_it_leaves_an_already_slugged_organization_alone(self, historical_organization_model):
        organization = historical_organization_model.objects.create(
            name="Hand Picked", slug="my-own-choice"
        )

        self._run(historical_organization_model)

        organization.refresh_from_db()
        assert organization.slug == "my-own-choice"


@pytest.mark.django_db
class TestSlugColumnAfterTheBackfill:
    def test_the_column_is_not_null(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT is_nullable, data_type, character_maximum_length
                FROM information_schema.columns
                WHERE table_name = 'organizations_organization' AND column_name = 'slug'
                """
            )
            is_nullable, data_type, max_length = cursor.fetchone()

        assert is_nullable == "NO"
        assert data_type == "character varying"
        assert max_length == 255

    def test_the_model_field_is_not_nullable(self):
        field = Organization._meta.get_field("slug")

        assert field.null is False
        assert field.unique is True

    def test_every_organization_has_a_non_null_slug(self):
        baker.make(Organization, _quantity=3)

        assert Organization.objects.filter(slug__isnull=True).count() == 0


@pytest.mark.django_db(transaction=True)
class TestBackfillMigrationNullBranch:
    """``0026``'s ``slug IS NULL`` branch, driven for real through ``MigrationExecutor``.

    By the time any other test in this module runs, ``0027`` has already made
    the column NOT NULL, so ``TestBackfillSlugsFunctionAgainstRealRows`` above
    can only reach the ``slug = ''`` half of ``UNSET_SLUG`` (one row at a time,
    since ``''`` is unique-constrained too). The branch that actually mattered
    on a real pre-1c database -- ``slug IS NULL``, which is *not*
    unique-constrained, so more than one row can hold it at once -- and the
    in-run ``taken`` set accumulating disambiguation across multiple such rows
    in the same pass, are otherwise unproven.

    Drives the executor back to ``0025_reparent_onto_package_abstract_bases``
    (the last migration before the column is touched at all -- still nullable,
    not yet backfilled), inserts two same-named rows with ``slug = NULL``
    directly against the historical model (which has no ``save()`` override,
    so nothing auto-derives a slug out from under the test), migrates forward
    again through ``0026`` and ``0027``, and asserts the disambiguated result.
    Mirrors ``tenancy/tests/test_seeded_database_migration_path.py`` and
    ``payments/tests/test_billing_period_summary_model.py::
    TestBillingPeriodSummaryMigration``'s precedent for driving
    ``MigrationExecutor`` directly against the shared per-worker test
    database, restored to head in a ``finally`` block regardless of where an
    assertion fails.
    """

    def test_two_null_slugged_rows_with_the_same_name_are_disambiguated(self):
        app_label = "tenancy"
        previous = "0025_reparent_onto_package_abstract_bases"

        executor = MigrationExecutor(connection)
        # The real head of the `tenancy` graph, computed rather than pinned to
        # `0027_organization_slug_not_null` by name -- a later migration in
        # this app must still come back fully applied when this test restores
        # state, not just the migration that existed when this test was
        # written.
        head = executor.loader.graph.leaf_nodes(app=app_label)[0]

        try:
            executor.migrate([(app_label, previous)])
            executor.loader.build_graph()

            historical_state = executor.loader.project_state((app_label, previous))
            historical_organization_model = historical_state.apps.get_model(
                "tenancy", "Organization"
            )

            first = historical_organization_model.objects.create(name="Null Slug Co", slug=None)
            second = historical_organization_model.objects.create(name="Null Slug Co", slug=None)
            assert first.slug is None
            assert second.slug is None
        finally:
            executor.migrate([head])
            executor.loader.build_graph()

        first_row = Organization.objects.get(pk=first.pk)
        second_row = Organization.objects.get(pk=second.pk)
        assert {first_row.slug, second_row.slug} == {"null-slug-co", "null-slug-co-2"}


@pytest.mark.django_db(transaction=True)
class TestSlugNotNullIsEnforcedByTheDatabase:
    def test_writing_a_null_slug_is_refused(self):
        organization = Organization.objects.create(name="Cannot Be Nulled")

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Organization.objects.filter(pk=organization.pk).update(slug=None)

    def test_two_organizations_cannot_share_a_slug(self):
        Organization.objects.create(name="Unique Slug Org", slug="unique-slug-org")

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                baker.make(Organization, slug="unique-slug-org")
