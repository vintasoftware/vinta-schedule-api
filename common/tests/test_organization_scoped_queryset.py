"""``OrganizationScopedQuerySet.update`` refuses to move a row between tenants.

The retired ``BaseOrganizationModelQuerySet.update`` raised on
``update(organization=...)`` / ``update(organization_id=...)``. The package's
``SingleOrganizationQuerySet.update`` does not replace that: it only takes care
*not to write* ``organization`` while rewriting a safe relation's kwargs onto the
concrete field, which says nothing about a caller naming the column outright.
Without the refusal, ``Calendar.objects.filter_by_organization(a).update(
organization_id=b)`` relocates rows across the tenant boundary and reports
success.

``Calendar`` is the model under test throughout because it also pins the second
half of this: ``CalendarQuerySet`` used to override ``update()`` with a body that
read ``self._meta`` (which ``QuerySet`` does not have) and called a classmethod
that left ``Calendar``'s MRO in Phase 2a -- so *any* ``Calendar.objects.filter(
...).update(...)`` raised ``AttributeError``, and the override shadowed the
package's working one. That override is gone; these tests are what stops it
coming back unnoticed.
"""

from __future__ import annotations

import pytest

from calendar_integration.models import Calendar, CalendarSync
from common.exceptions import OrganizationCannotBeUpdatedError
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
