"""``.update()`` on this project's queryset reaches the package's refusal.

What is pinned here is *wiring*, not the rule itself. Package ``0.4.0`` owns
the rule -- ``SingleOrganizationQuerySet.update`` raises
``vinta_orgs.exceptions.OrganizationCannotBeUpdatedError`` when the rewritten
kwargs name ``organization`` / ``organization_id`` -- and the package tests it.
What this project can still break is the path to it, which it has broken twice:

* ``common.querysets.OrganizationScopedQuerySet`` used to carry an ``update()``
  override that raised a *same-named* exception from an unrelated hierarchy and
  fired before delegating, so ``except`` clauses split in two and
  ``unsafe_organization_update=True`` was unreachable. The assertions below name
  the package's class deliberately: they fail if a local override that raises
  something else comes back.
* ``CalendarQuerySet`` used to override ``update()`` with a body that read
  ``self._meta`` (which ``QuerySet`` does not have) and called a classmethod
  that left ``Calendar``'s MRO in Phase 2a -- so *any*
  ``Calendar.objects.filter(...).update(...)`` raised ``AttributeError``, and
  the override shadowed the package's working one. ``Calendar`` is the model
  under test throughout for that reason.

The opt-in is exercised too: a shadowing override would swallow the
``unsafe_organization_update`` keyword and turn the escape hatch this project
reserves for data migrations into a ``TypeError``.

The instance-level half of the same rule -- ``save()`` on a persisted row --
lives in ``common/tests/test_organization_immutability.py``.
"""

from __future__ import annotations

import pytest
from vinta_orgs.exceptions import OrganizationCannotBeUpdatedError

from calendar_integration.models import Calendar, CalendarSync
from organizations.models import Organization


pytestmark = pytest.mark.django_db


@pytest.fixture
def organization_a() -> Organization:
    return Organization.objects.create(name="Org A")


@pytest.fixture
def organization_b() -> Organization:
    return Organization.objects.create(name="Org B")


@pytest.fixture
def calendar_a(organization_a: Organization) -> Calendar:
    return Calendar.objects.create(name="A's calendar", organization=organization_a)


class TestUpdateRefusesToRelocateRows:
    def test_naming_the_organization_instance_raises(
        self, organization_a, organization_b, calendar_a
    ):
        with pytest.raises(OrganizationCannotBeUpdatedError):
            Calendar.objects.filter_by_organization(organization_a.id).update(
                organization=organization_b
            )

        calendar_a.refresh_from_db()
        assert calendar_a.organization_id == organization_a.id

    def test_naming_the_organization_id_raises(self, organization_a, organization_b, calendar_a):
        with pytest.raises(OrganizationCannotBeUpdatedError):
            Calendar.objects.filter_by_organization(organization_a.id).update(
                organization_id=organization_b.id
            )

        calendar_a.refresh_from_db()
        assert calendar_a.organization_id == organization_a.id

    def test_the_refusal_fires_before_the_statement_runs(
        self, organization_a, organization_b, calendar_a
    ):
        """Nothing else in the same ``update()`` is written either -- the refusal
        is not a post-hoc check on a statement that already ran.
        """
        with pytest.raises(OrganizationCannotBeUpdatedError):
            Calendar.objects.filter_by_organization(organization_a.id).update(
                name="renamed", organization_id=organization_b.id
            )

        calendar_a.refresh_from_db()
        assert calendar_a.name == "A's calendar"

    def test_the_opt_in_reaches_the_package_and_relocates_the_row(
        self, organization_a, organization_b, calendar_a
    ):
        """``unsafe_organization_update=True`` is reachable through ``.update()``.

        The plan reserves this flag for a data migration that genuinely has to
        re-stamp rows, and says the flag belongs at that call site. A local
        override that raised before delegating made it unreachable -- the
        refusal fired regardless of the keyword -- so this is what proves the
        escape hatch exists rather than only being documented.
        """
        assert (
            Calendar.objects.filter_by_organization(organization_a.id).update(
                organization_id=organization_b.id, unsafe_organization_update=True
            )
            == 1
        )

        calendar_a.refresh_from_db()
        assert calendar_a.organization_id == organization_b.id


class TestTheProjectAliasIsThePackageClass:
    def test_common_exceptions_re_exports_the_package_exception(self):
        """One class, so one ``except`` clause covers all five write paths.

        ``common.exceptions`` used to declare a ``CommonError`` subclass of the
        same name and the same default message. Code written against either one
        silently missed the other, and ``save()`` / ``bulk_update()`` /
        ``update_or_create()`` / conflict-updating ``bulk_create()`` only ever
        raised the package's.
        """
        import common.exceptions

        assert (
            common.exceptions.OrganizationCannotBeUpdatedError is OrganizationCannotBeUpdatedError
        )


class TestUpdateOtherwiseBehavesAsThePackageIntends:
    def test_an_ordinary_column_updates(self, organization_a, calendar_a):
        assert (
            Calendar.objects.filter_by_organization(organization_a.id).update(name="renamed") == 1
        )

        calendar_a.refresh_from_db()
        assert (calendar_a.name, calendar_a.organization_id) == ("renamed", organization_a.id)

    def test_a_safe_relation_is_rewritten_onto_its_concrete_field(self, organization_a, calendar_a):
        """``update(calendar=...)`` names the non-concrete half of the safe
        relation, which Django cannot write. The package's ``update()`` points it
        at ``calendar_fk`` -- behaviour the deleted ``CalendarQuerySet.update``
        was shadowing on ``Calendar`` and reimplementing (broken) for it.
        """
        other_calendar = Calendar.objects.create(
            name="Second", external_id="second", organization=organization_a
        )
        sync = CalendarSync.objects.create(
            calendar=calendar_a,
            organization=organization_a,
            start_datetime="2025-06-22T00:00:00Z",
            end_datetime="2025-06-22T23:59:00Z",
            should_update_events=True,
        )

        CalendarSync.objects.filter_by_organization(organization_a.id).filter(pk=sync.pk).update(
            calendar=other_calendar
        )

        sync.refresh_from_db()
        assert sync.calendar_fk_id == other_calendar.id
        assert sync.organization_id == organization_a.id
