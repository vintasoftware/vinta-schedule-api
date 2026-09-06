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
    AppointmentTypeSlotMembership,
    Calendar,
)
from calendar_integration.services.appointment_type_service import AppointmentTypeService
from calendar_integration.services.dataclasses import (
    AppointmentTypeInputData,
    AppointmentTypeSlotInputData,
)
from organizations.models import Organization


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Reconcile Org", should_sync_rooms=False)


@pytest.fixture
def service(organization):
    svc = AppointmentTypeService()
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
def appointment_type(service, calendars, pool):
    return service.create_appointment_type(
        AppointmentTypeInputData(
            name="Clinic Appointments",
            description="",
            slots=[
                AppointmentTypeSlotInputData(
                    name="Physicians",
                    calendar_ids=[calendars["phys_a"].id],
                    pool_ids=[pool.id],
                    order=0,
                ),
                AppointmentTypeSlotInputData(
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
def test_clean_projection_reports_no_drift(appointment_type, organization):
    output = _run()

    assert "no drift found" in output
    assert "DRIFT DETECTED" not in output


@pytest.mark.django_db
def test_missing_projected_row_is_detected_and_repaired(
    appointment_type, organization, calendars, pool
):
    physicians = appointment_type.slots.get(name="Physicians")
    # Corrupt the projection: drop the row the attachment implies.
    AppointmentTypeSlotMembership.objects.filter_by_organization(
        organization.id
    ).projected().filter(slot_fk=physicians, calendar_fk=calendars["phys_b"]).delete()

    dry_run_output = _run()

    assert "DRIFT DETECTED" in dry_run_output
    assert f"MISSING slot={physicians.id} pool={pool.id}" in dry_run_output
    assert "Dry run: nothing was written" in dry_run_output
    # Still corrupt -- the default run wrote nothing.
    assert (
        not AppointmentTypeSlotMembership.objects.filter_by_organization(organization.id)
        .projected()
        .filter(slot_fk=physicians, calendar_fk=calendars["phys_b"])
        .exists()
    )

    fix_output = _run(fix=True)

    assert "DRIFT DETECTED" in fix_output
    assert "Dry run" not in fix_output
    repaired = (
        AppointmentTypeSlotMembership.objects.filter_by_organization(organization.id)
        .projected()
        .filter(slot_fk=physicians, calendar_fk=calendars["phys_b"])
    )
    assert repaired.count() == 1
    assert repaired.get().source_pool_fk_id == pool.id
    assert "no drift found" in _run()


@pytest.mark.django_db
def test_orphaned_projected_row_is_detected_and_repaired(
    appointment_type, organization, calendars, pool
):
    physicians = appointment_type.slots.get(name="Physicians")
    # Corrupt the projection the other way: a projected row for a calendar the
    # pool does not roster.
    AppointmentTypeSlotMembership.objects.create(
        organization=organization,
        slot=physicians,
        calendar=calendars["room_1"],
        source_pool=pool,
    )

    dry_run_output = _run()

    assert f"ORPHANED slot={physicians.id} pool={pool.id}" in dry_run_output

    _run(fix=True)

    assert (
        not AppointmentTypeSlotMembership.objects.filter_by_organization(organization.id)
        .projected()
        .filter(slot_fk=physicians, calendar_fk=calendars["room_1"])
        .exists()
    )
    assert "no drift found" in _run()


@pytest.mark.django_db
def test_repair_never_touches_inline_rows(appointment_type, organization, calendars, pool):
    """An inline row for a calendar no pool rosters is not "orphaned"."""
    physicians = appointment_type.slots.get(name="Physicians")
    inline_ids = set(
        AppointmentTypeSlotMembership.objects.filter_by_organization(organization.id)
        .inline()
        .values_list("id", flat=True)
    )
    # Drop the pool's whole roster so every projected row it implies disappears;
    # the inline rows must survive the repair untouched.
    pool.memberships.all().delete()

    _run(fix=True)

    assert (
        set(
            AppointmentTypeSlotMembership.objects.filter_by_organization(organization.id)
            .inline()
            .values_list("id", flat=True)
        )
        == inline_ids
    )
    assert (
        AppointmentTypeSlotMembership.objects.filter_by_organization(organization.id)
        .projected()
        .filter(slot_fk=physicians)
        .count()
        == 0
    )


@pytest.mark.django_db
def test_organization_without_pools_reports_clean(service, calendars):
    service.create_appointment_type(
        AppointmentTypeInputData(
            name="No Pools Here",
            description="",
            slots=[
                AppointmentTypeSlotInputData(
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


@pytest.mark.django_db
def test_fix_across_all_organizations_leaves_a_clean_organization_byte_identical(
    appointment_type, organization, calendars, pool
):
    """The multi-organization loop: corrupting org A's projection and running
    `--fix` with no `--organization-id` filter must repair only org A. Org B's
    rows -- clean from the start -- must come out byte-identical."""
    other_org = Organization.objects.create(name="Other Reconcile Org", should_sync_rooms=False)
    other_calendars = {}
    for name, external in (("Dr. X", "other_phys_x"), ("Dr. Y", "other_phys_y")):
        other_calendars[external] = Calendar.objects.create(
            organization=other_org,
            name=name,
            external_id=external,
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=True,
        )
    other_pool = create_calendar_pool(
        organization=other_org,
        name="Other Nurses",
        calendars=[other_calendars["other_phys_y"]],
    )
    other_service = AppointmentTypeService()
    other_service.initialize(organization=other_org)
    other_appointment_type = other_service.create_appointment_type(
        AppointmentTypeInputData(
            name="Other Clinic",
            description="",
            slots=[
                AppointmentTypeSlotInputData(
                    name="Physicians",
                    calendar_ids=[other_calendars["other_phys_x"].id],
                    pool_ids=[other_pool.id],
                    order=0,
                ),
            ],
        )
    )
    other_physicians = other_appointment_type.slots.get(name="Physicians")
    other_rows_before = list(
        AppointmentTypeSlotMembership.objects.filter_by_organization(other_org.id)
        .filter(slot_fk=other_physicians)
        .order_by("id")
        .values("id", "calendar_fk_id", "source_pool_fk_id")
    )
    assert other_rows_before  # sanity: org B actually has rows to compare.

    # Corrupt only org A's projection.
    physicians = appointment_type.slots.get(name="Physicians")
    AppointmentTypeSlotMembership.objects.filter_by_organization(
        organization.id
    ).projected().filter(slot_fk=physicians, calendar_fk=calendars["phys_b"]).delete()

    output = _run(fix=True)

    assert f"org={organization.id}" in output
    assert f"org={other_org.id}" not in output
    # Org A repaired.
    assert (
        AppointmentTypeSlotMembership.objects.filter_by_organization(organization.id)
        .projected()
        .filter(slot_fk=physicians, calendar_fk=calendars["phys_b"])
        .count()
        == 1
    )
    # Org B untouched -- byte-identical to what it was before the sweep.
    other_rows_after = list(
        AppointmentTypeSlotMembership.objects.filter_by_organization(other_org.id)
        .filter(slot_fk=other_physicians)
        .order_by("id")
        .values("id", "calendar_fk_id", "source_pool_fk_id")
    )
    assert other_rows_after == other_rows_before


@pytest.mark.django_db
def test_dry_run_and_fix_together_is_a_command_error(
    appointment_type, organization, calendars, pool
):
    """`--dry-run --fix` used to silently take --fix's write despite the flag
    whose whole story is "this is safe to run." Passing both is refused."""
    from django.core.management.base import CommandError

    physicians = appointment_type.slots.get(name="Physicians")
    AppointmentTypeSlotMembership.objects.filter_by_organization(
        organization.id
    ).projected().filter(slot_fk=physicians, calendar_fk=calendars["phys_b"]).delete()

    with pytest.raises(CommandError, match="mutually exclusive"):
        _run(dry_run=True, fix=True)

    # Refused before any write -- the corrupted projection is untouched.
    assert (
        not AppointmentTypeSlotMembership.objects.filter_by_organization(organization.id)
        .projected()
        .filter(slot_fk=physicians, calendar_fk=calendars["phys_b"])
        .exists()
    )
