"""Organization ownership is immutable on a persisted row, and it is free.

Package ``0.4.0`` moved the tenant boundary from "do not write ``organization``
in bulk" to "an existing row cannot change organization at all".
``SingleOrganizationModelMixin.save()`` compares the instance's
``organization_id`` against the persisted one and raises
``vinta_orgs.exceptions.OrganizationCannotBeUpdatedError`` on a mismatch.

**Why this file exists.** Nothing in this repo needs
``unsafe_organization_update=True`` -- an audited fact -- and a miss would
surface as a runtime error in production rather than as a test failure --
which is exactly the case for pinning the rule here instead of trusting the
audit. All 34 scoped models
inherit this behaviour from one mixin, so ``Calendar`` and ``CalendarSync``
stand in for the set.

Two things are pinned:

1. **The refusal fires, and only on a genuine relocation.** A save that changes
   the organization raises; an ordinary save of the same row does not. Without
   the second half the first is satisfied by a mixin that raises on every save.
2. **What the refusal costs on the write path: nothing.** ``0.4.0`` answered
   the check with a ``SELECT ... FOR UPDATE`` inside ``transaction.atomic()``
   on *every* save of a persisted scoped row where the organization was among
   the columns written -- and under ``ATOMIC_REQUESTS = True`` that savepoint
   plus locking read held its row lock until the *request* transaction
   committed, not until the save returned. ``0.5.0`` records the persisted
   ``organization_id`` in ``from_db`` and compares in memory instead, so a save
   that leaves the organization alone is the single ``UPDATE`` it always was.

   These query-count assertions are what made that change visible: they were
   written against ``0.4.0``'s lock and went red on the bump. The rest of this
   suite's query counts are all read-path, so nothing else would have noticed.
"""

from __future__ import annotations

from django.db import connection
from django.test.utils import CaptureQueriesContext

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


def _locking_reads(captured: CaptureQueriesContext) -> list[str]:
    return [query["sql"] for query in captured.captured_queries if "FOR UPDATE" in query["sql"]]


class TestSaveRefusesToRelocateAPersistedRow:
    def test_restamping_the_organization_and_saving_raises(
        self, organization_a, organization_b, calendar_a
    ):
        calendar_a.organization = organization_b

        with pytest.raises(OrganizationCannotBeUpdatedError):
            calendar_a.save()

        assert (
            Calendar.objects.filter_by_organization(organization_a.id)
            .filter(pk=calendar_a.pk)
            .exists()
        )

    def test_restamping_only_the_id_and_saving_raises(
        self, organization_a, organization_b, calendar_a
    ):
        """The id spelling is the one that skips the descriptor, so it is the
        one a bulk fix-up script would reach for. It is refused too.
        """
        calendar_a.organization_id = organization_b.id

        with pytest.raises(OrganizationCannotBeUpdatedError):
            calendar_a.save()

        calendar_a.refresh_from_db()
        assert calendar_a.organization_id == organization_a.id

    def test_naming_the_organization_in_update_fields_raises(
        self, organization_a, organization_b, calendar_a
    ):
        calendar_a.organization = organization_b

        with pytest.raises(OrganizationCannotBeUpdatedError):
            calendar_a.save(update_fields=["organization"])

        calendar_a.refresh_from_db()
        assert calendar_a.organization_id == organization_a.id

    def test_an_ordinary_save_of_the_same_row_does_not_raise(self, organization_a, calendar_a):
        """The other half of the gate.

        Without this, a mixin that raised on *every* save of a persisted scoped
        row would still satisfy every assertion above.
        """
        calendar_a.name = "renamed"
        calendar_a.save()

        calendar_a.refresh_from_db()
        assert (calendar_a.name, calendar_a.organization_id) == ("renamed", organization_a.id)

    def test_a_save_that_re_asserts_the_same_organization_does_not_raise(
        self, organization_a, calendar_a
    ):
        """Re-stamping is only refused when the organization actually changes.

        Services that rebuild an instance and set ``organization`` from the
        current context write the value they already had; that must stay a
        plain ``UPDATE``.
        """
        calendar_a.organization = organization_a
        calendar_a.name = "re-stamped"
        calendar_a.save()

        calendar_a.refresh_from_db()
        assert (calendar_a.name, calendar_a.organization_id) == ("re-stamped", organization_a.id)

    def test_the_opt_in_relocates_the_row(self, organization_a, organization_b, calendar_a):
        """``unsafe_organization_update=True`` is the escape hatch reserved
        for a data migration, and it is reachable through ``save()``.

        It also proves the refusals above are *this* rule and not some unrelated
        failure that happens to raise the same class.
        """
        calendar_a.organization = organization_b
        calendar_a.save(unsafe_organization_update=True)

        calendar_a.refresh_from_db()
        assert calendar_a.organization_id == organization_b.id


class TestWhatTheRefusalCostsOnTheWritePath:
    """Characterized so an upstream change to the write path is visible.

    Under ``0.4.0`` these pinned a locking read. They went red on the ``0.5.0``
    bump, which is what they exist for; they now pin its absence.
    """

    def test_an_unrestricted_save_of_a_persisted_row_takes_no_row_lock(self, calendar_a):
        """One statement, the same as an unscoped model would issue.

        ``0.4.0`` issued four here -- ``SAVEPOINT``, ``SELECT ... FOR UPDATE``,
        ``UPDATE``, ``RELEASE SAVEPOINT`` -- because the check read the
        persisted organization back. The instance carries it now.
        """
        calendar_a.name = "renamed"

        with CaptureQueriesContext(connection) as captured:
            calendar_a.save()

        assert _locking_reads(captured) == []
        assert len(captured.captured_queries) == 1
        assert captured.captured_queries[0]["sql"].split()[0] == "UPDATE"

    def test_update_fields_that_exclude_the_organization_take_no_lock(self, calendar_a):
        """The narrow save is the cheap one: no savepoint, no locking read."""
        calendar_a.name = "renamed"

        with CaptureQueriesContext(connection) as captured:
            calendar_a.save(update_fields=["name"])

        assert _locking_reads(captured) == []
        assert len(captured.captured_queries) == 1

    def test_update_fields_naming_a_safe_relation_is_still_checked(
        self, organization_a, calendar_a
    ):
        """``update_fields=['calendar']`` is *not* a narrow save.

        ``expand_safe_relation_field_names`` turns an ``OrganizationSafeForeignKey``
        name into ``calendar_fk`` **and** ``organization`` -- assigning the
        relation sets both -- so the organization is among the columns written
        and the immutability check applies. Under ``0.4.0`` that meant this
        seemingly narrow save took the lock too, which is why characterizing the
        cost as "``update_fields is None``" understated it. The check still
        applies; it no longer reads or locks.
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
        sync.calendar = other_calendar

        with CaptureQueriesContext(connection) as captured:
            sync.save(update_fields=["calendar"])

        assert _locking_reads(captured) == []

        sync.refresh_from_db()
        assert sync.calendar_fk_id == other_calendar.id

    def test_an_insert_takes_no_lock(self, organization_a):
        """There is no persisted row to compare against, so nothing is locked."""
        with CaptureQueriesContext(connection) as captured:
            Calendar.objects.create(name="Fresh", organization=organization_a)

        assert _locking_reads(captured) == []
