"""Tests for the ``reconcile_calendar_pool_projections`` drift sweep.

The roster projection is written rather than computed, so it can drift from the
pools it derives from. This command is the plan's mitigation: it recomputes the
projected half from scratch, reports differences, and repairs them only when
asked. Two properties matter and are pinned below: ``--dry-run`` is the default
(so a bare run cannot write), and inline rows are outside its world entirely (so
a repair can never delete a hand-curated roster entry).
"""

from io import StringIO

from django.core.management import call_command

import pytest

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.factories import create_calendar_pool
from calendar_integration.models import (
    Calendar,
    CalendarGroupSlotMembership,
)
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.dataclasses import (
    CalendarGroupInputData,
    CalendarGroupSlotInputData,
)
from organizations.models import Organization


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Reconcile Org", should_sync_rooms=False)


@pytest.fixture
def service(organization):
    svc = CalendarGroupService()
    svc.initialize(organization=organization)
    return svc


@pytest.fixture
def calendars(organization):
    made = {}
    for name, external in (("Dr. A", "phys_a"), ("Dr. B", "phys_b"), ("Room 1", "room_1")):
        made[external] = Calendar.objects.create(
            organization=organization,
            name=name,
            external_id=external,
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=True,
        )
    return made


@pytest.fixture
def pool(organization, calendars):
    return create_calendar_pool(
        organization=organization,
        name="Nurses",
        calendars=[calendars["phys_b"]],
    )


@pytest.fixture
def group(service, calendars, pool):
    return service.create_group(
        CalendarGroupInputData(
            name="Clinic Appointments",
            description="",
            slots=[
                CalendarGroupSlotInputData(
                    name="Physicians",
                    calendar_ids=[calendars["phys_a"].id],
                    pool_ids=[pool.id],
                    order=0,
                ),
                CalendarGroupSlotInputData(
                    name="Rooms",
                    calendar_ids=[calendars["room_1"].id],
                    order=1,
                ),
            ],
        )
    )


def _run(**options) -> str:
    out = StringIO()
    call_command("reconcile_calendar_pool_projections", stdout=out, **options)
    return out.getvalue()


@pytest.mark.django_db
def test_clean_projection_reports_no_drift(group, organization):
    output = _run()

    assert "no drift found" in output
    assert "DRIFT DETECTED" not in output


@pytest.mark.django_db
def test_missing_projected_row_is_detected_and_repaired(group, organization, calendars, pool):
    physicians = group.slots.get(name="Physicians")
    # Corrupt the projection: drop the row the attachment implies.
    CalendarGroupSlotMembership.objects.filter_by_organization(organization.id).projected().filter(
        slot_fk=physicians, calendar_fk=calendars["phys_b"]
    ).delete()

    dry_run_output = _run()

    assert "DRIFT DETECTED" in dry_run_output
    assert f"MISSING slot={physicians.id} pool={pool.id}" in dry_run_output
    assert "Dry run: nothing was written" in dry_run_output
    # Still corrupt -- the default run wrote nothing.
    assert (
        not CalendarGroupSlotMembership.objects.filter_by_organization(organization.id)
        .projected()
        .filter(slot_fk=physicians, calendar_fk=calendars["phys_b"])
        .exists()
    )

    fix_output = _run(fix=True)

    assert "DRIFT DETECTED" in fix_output
    assert "Dry run" not in fix_output
    repaired = (
        CalendarGroupSlotMembership.objects.filter_by_organization(organization.id)
        .projected()
        .filter(slot_fk=physicians, calendar_fk=calendars["phys_b"])
    )
    assert repaired.count() == 1
    assert repaired.get().source_pool_fk_id == pool.id
    assert "no drift found" in _run()


@pytest.mark.django_db
def test_orphaned_projected_row_is_detected_and_repaired(group, organization, calendars, pool):
    physicians = group.slots.get(name="Physicians")
    # Corrupt the projection the other way: a projected row for a calendar the
    # pool does not roster.
    CalendarGroupSlotMembership.objects.create(
        organization=organization,
        slot=physicians,
        calendar=calendars["room_1"],
        source_pool=pool,
    )

    dry_run_output = _run()

    assert f"ORPHANED slot={physicians.id} pool={pool.id}" in dry_run_output

    _run(fix=True)

    assert (
        not CalendarGroupSlotMembership.objects.filter_by_organization(organization.id)
        .projected()
        .filter(slot_fk=physicians, calendar_fk=calendars["room_1"])
        .exists()
    )
    assert "no drift found" in _run()


@pytest.mark.django_db
def test_repair_never_touches_inline_rows(group, organization, calendars, pool):
    """An inline row for a calendar no pool rosters is not "orphaned"."""
    physicians = group.slots.get(name="Physicians")
    inline_ids = set(
        CalendarGroupSlotMembership.objects.filter_by_organization(organization.id)
        .inline()
        .values_list("id", flat=True)
    )
    # Drop the pool's whole roster so every projected row it implies disappears;
    # the inline rows must survive the repair untouched.
    pool.memberships.all().delete()

    _run(fix=True)

    assert (
        set(
            CalendarGroupSlotMembership.objects.filter_by_organization(organization.id)
            .inline()
            .values_list("id", flat=True)
        )
        == inline_ids
    )
    assert (
        CalendarGroupSlotMembership.objects.filter_by_organization(organization.id)
        .projected()
        .filter(slot_fk=physicians)
        .count()
        == 0
    )


@pytest.mark.django_db
def test_organization_without_pools_reports_clean(service, calendars):
    service.create_group(
        CalendarGroupInputData(
            name="No Pools Here",
            description="",
            slots=[
                CalendarGroupSlotInputData(
                    name="Physicians",
                    calendar_ids=[calendars["phys_a"].id],
                    order=0,
                )
            ],
        )
    )

    assert "no drift found" in _run()


@pytest.mark.django_db
def test_unknown_organization_id_is_a_command_error(db):
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="does not exist"):
        _run(organization_id=999_999)
