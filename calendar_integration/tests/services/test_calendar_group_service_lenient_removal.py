"""Calendar Pools Phase 1: roster removal is lenient.

Removing a calendar from a slot's roster always succeeds, keeps every
existing event's calendar selections intact (past and future), and preserves
that calendar's group-scoped windows, blocked time, and quota rules -- which
keep enforcing on a subsequent reschedule of a grandfathered booking. Roster
membership is validated only against calendars being ADDED to an event;
calendars already recorded on that event pass through untouched, and the
create path (where every selection is an addition) is unchanged.
"""

from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

import pytest

from audit_integration.constants import AuditAction
from audit_integration.services import OrganizationAuditService
from calendar_integration.constants import CalendarProvider, CalendarType, QuotaPeriod
from calendar_integration.exceptions import (
    CalendarGroupScopedRuleViolationError,
    CalendarGroupValidationError,
)
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroupSlotMembership,
    CalendarGroupSlotQuotaRule,
    CalendarManagementToken,
)
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.calendar_permission_service import (
    DEFAULT_CALENDAR_OWNER_PERMISSIONS,
    CalendarPermissionService,
)
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    CalendarGroupEventInputData,
    CalendarGroupInputData,
    CalendarGroupSlotInputData,
    CalendarGroupSlotSelectionInputData,
)
from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN
from organizations.tests.helpers import grant_membership_groups
from users.models import Profile, User


# ---------------------------------------------------------------------------
# Fixtures -- plain roster/service pair (no calendar_service), mirroring
# test_calendar_group_service.py's `service` + `managed_calendars`. Used for
# the update_group / _reconcile_slot behavior and group-scoped row survival.
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Lenient Removal Org", should_sync_rooms=False)


@pytest.fixture
def audit_service() -> OrganizationAuditService:
    from di_core.containers import get_container

    return get_container().audit_service()


@pytest.fixture
def admin_user(db, organization):
    u = User.objects.create_user(email="admin@example.com", password="pass")
    Profile.objects.create(user=u)
    grant_membership_groups(
        OrganizationMembership.objects.create(user=u, organization=organization),
        [GROUP_ORGANIZATION_ADMIN],
    )
    return u


@pytest.fixture
def service(organization, audit_service):
    svc = CalendarGroupService(
        audit_service=audit_service,
        calendar_permission_service=CalendarPermissionService(),
    )
    svc.initialize(organization=organization)
    return svc


@pytest.fixture
def managed_calendars(organization):
    calendars = {}
    for name, external in (
        ("Dr. A", "phys_a"),
        ("Dr. B", "phys_b"),
        ("Room 1", "room_1"),
    ):
        calendars[external] = Calendar.objects.create(
            organization=organization,
            name=name,
            external_id=external,
            provider=CalendarProvider.GOOGLE,
            calendar_type=(
                CalendarType.PERSONAL if external.startswith("phys_") else CalendarType.RESOURCE
            ),
            manage_available_windows=True,
        )
    return calendars


@pytest.fixture
def base_input(managed_calendars):
    return CalendarGroupInputData(
        name="Clinic Appointments",
        description="",
        slots=[
            CalendarGroupSlotInputData(
                name="Physicians",
                calendar_ids=[managed_calendars["phys_a"].id, managed_calendars["phys_b"].id],
                required_count=1,
                order=0,
            ),
            CalendarGroupSlotInputData(
                name="Rooms",
                calendar_ids=[managed_calendars["room_1"].id],
                required_count=1,
                order=1,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Roster removal never fails and never destroys existing selections
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_removing_calendar_with_future_booking_succeeds(service, base_input, managed_calendars):
    """Contract change: this previously raised CalendarGroupSlotInUseError."""
    group = service.create_group(base_input)
    physicians = group.slots.get(name="Physicians")
    future_event = CalendarEvent.objects.create(
        organization=service.organization,
        calendar_fk=managed_calendars["phys_a"],
        title="Future appointment",
        description="",
        external_id="ev_future",
        start_time_tz_unaware=timezone.now() + timedelta(days=2),
        end_time_tz_unaware=timezone.now() + timedelta(days=2, hours=1),
        timezone="UTC",
        calendar_group_fk=group,
    )
    CalendarEventGroupSelection.objects.create(
        organization=service.organization,
        event=future_event,
        slot=physicians,
        calendar=managed_calendars["phys_a"],
    )

    base_input.slots[0].calendar_ids = [managed_calendars["phys_b"].id]

    updated = service.update_group(group.id, base_input)

    physicians = updated.slots.get(name="Physicians")
    assert set(physicians.calendars.values_list("external_id", flat=True)) == {"phys_b"}
    assert (
        not CalendarGroupSlotMembership.objects.filter_by_organization(service.organization.id)
        .filter(slot_fk=physicians, calendar_fk=managed_calendars["phys_a"])
        .exists()
    )


@pytest.mark.django_db
def test_removing_calendar_keeps_past_and_future_selections(service, base_input, managed_calendars):
    group = service.create_group(base_input)
    physicians = group.slots.get(name="Physicians")

    future_event = CalendarEvent.objects.create(
        organization=service.organization,
        calendar_fk=managed_calendars["phys_a"],
        title="Future appointment",
        description="",
        external_id="ev_future",
        start_time_tz_unaware=timezone.now() + timedelta(days=2),
        end_time_tz_unaware=timezone.now() + timedelta(days=2, hours=1),
        timezone="UTC",
        calendar_group_fk=group,
    )
    past_event = CalendarEvent.objects.create(
        organization=service.organization,
        calendar_fk=managed_calendars["phys_a"],
        title="Past appointment",
        description="",
        external_id="ev_past",
        start_time_tz_unaware=timezone.now() - timedelta(days=2),
        end_time_tz_unaware=timezone.now() - timedelta(days=2) + timedelta(hours=1),
        timezone="UTC",
        calendar_group_fk=group,
    )
    for event in (future_event, past_event):
        CalendarEventGroupSelection.objects.create(
            organization=service.organization,
            event=event,
            slot=physicians,
            calendar=managed_calendars["phys_a"],
        )

    base_input.slots[0].calendar_ids = [managed_calendars["phys_b"].id]
    service.update_group(group.id, base_input)

    selections = CalendarEventGroupSelection.objects.filter_by_organization(
        service.organization.id
    ).filter(calendar_fk=managed_calendars["phys_a"])
    assert selections.filter(event_fk=future_event).exists()
    assert selections.filter(event_fk=past_event).exists()


@pytest.mark.django_db
def test_removed_calendar_scoped_rows_survive_and_readd_restores_config(
    service, admin_user, base_input, managed_calendars
):
    """The removed calendar's group-scoped AvailableTime, BlockedTime, and
    CalendarGroupSlotQuotaRule rows still exist after removal, and re-adding
    the calendar to the roster leaves them exactly as they were."""
    group = service.create_group(base_input)
    physicians = group.slots.get(name="Physicians")

    window_result = service.create_group_scoped_availability_window(
        acting_user=admin_user,
        group_slot_id=physicians.id,
        calendar_id=managed_calendars["phys_a"].id,
        start_time=timezone.now() + timedelta(days=3),
        end_time=timezone.now() + timedelta(days=3, hours=8),
        tz="UTC",
    )
    window_id = window_result.window.id  # type: ignore[union-attr]

    block_result = service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=physicians.id,
        calendar_id=managed_calendars["phys_a"].id,
        start_time=timezone.now() + timedelta(days=3, hours=1),
        end_time=timezone.now() + timedelta(days=3, hours=2),
        tz="UTC",
    )
    block_id = block_result.block.id  # type: ignore[union-attr]

    rule = service.create_group_scoped_quota_rule(
        acting_user=admin_user,
        group_slot_id=physicians.id,
        calendar_id=managed_calendars["phys_a"].id,
        period=QuotaPeriod.DAY,
        cap=1,
    )

    # Remove phys_a from the roster.
    base_input.slots[0].calendar_ids = [managed_calendars["phys_b"].id]
    service.update_group(group.id, base_input)

    assert AvailableTime.objects.unscoped().filter(id=window_id).exists()
    assert BlockedTime.objects.unscoped().filter(id=block_id).exists()
    assert (
        CalendarGroupSlotQuotaRule.objects.filter_by_organization(service.organization.id)
        .filter(id=rule.id)
        .exists()
    )

    # Re-add phys_a -- its group-scoped configuration is untouched.
    base_input.slots[0].calendar_ids = [
        managed_calendars["phys_a"].id,
        managed_calendars["phys_b"].id,
    ]
    updated = service.update_group(group.id, base_input)

    physicians = updated.slots.get(name="Physicians")
    assert set(physicians.calendars.values_list("external_id", flat=True)) == {"phys_a", "phys_b"}
    window = AvailableTime.objects.unscoped().get(id=window_id)
    assert window.calendar_fk_id == managed_calendars["phys_a"].id
    block = BlockedTime.objects.unscoped().get(id=block_id)
    assert block.calendar_fk_id == managed_calendars["phys_a"].id
    rule.refresh_from_db()
    assert rule.cap == 1
    assert rule.period == QuotaPeriod.DAY


@pytest.mark.django_db
def test_readd_then_create_same_period_quota_rule_fails(
    service, admin_user, base_input, managed_calendars
):
    """Re-adding a calendar to a roster restores its previous quota rules --
    they were never deleted on removal (see
    `test_removed_calendar_scoped_rows_survive_and_readd_restores_config`).
    So a second create for the same (calendar, slot, period) after a
    remove/re-add round trip now fails on the pre-existing rule's uniqueness
    constraint, where it would have succeeded under the old, destructive
    removal behavior (which deleted the rule along with the membership)."""
    group = service.create_group(base_input)
    physicians = group.slots.get(name="Physicians")

    service.create_group_scoped_quota_rule(
        acting_user=admin_user,
        group_slot_id=physicians.id,
        calendar_id=managed_calendars["phys_a"].id,
        period=QuotaPeriod.DAY,
        cap=1,
    )

    # Remove phys_a from the roster, then re-add her "a week later".
    base_input.slots[0].calendar_ids = [managed_calendars["phys_b"].id]
    service.update_group(group.id, base_input)

    base_input.slots[0].calendar_ids = [
        managed_calendars["phys_a"].id,
        managed_calendars["phys_b"].id,
    ]
    service.update_group(group.id, base_input)

    with pytest.raises(CalendarGroupValidationError):
        service.create_group_scoped_quota_rule(
            acting_user=admin_user,
            group_slot_id=physicians.id,
            calendar_id=managed_calendars["phys_a"].id,
            period=QuotaPeriod.DAY,
            cap=1,
        )


@pytest.mark.django_db
def test_removing_calendar_from_roster_does_not_free_availability_windows_capacity(
    service, admin_user, base_input, managed_calendars
):
    """Billing / limits: the plan's Risk & Rollout Notes calls this out
    explicitly -- scoped rows now outlive roster membership, so the
    `availability_windows` metered counter (which reads through
    `AvailableTime.objects.unscoped().only_user_authored()`, uncorrelated with
    slot-roster membership) no longer drops when an admin removes a calendar
    from a slot's roster the way it did when removal deleted those rows. This
    must be asserted here rather than discovered on an invoice.
    """
    from vinta_billing.counting import UsageContext
    from vinta_billing.registry import resources

    import payments.seams.resources  # noqa: F401 -- ensure registration; see payments/tests/seams/test_resources.py
    from payments.seams.resource_keys import AVAILABILITY_WINDOWS

    group = service.create_group(base_input)
    physicians = group.slots.get(name="Physicians")

    for offset in range(3):
        service.create_group_scoped_availability_window(
            acting_user=admin_user,
            group_slot_id=physicians.id,
            calendar_id=managed_calendars["phys_a"].id,
            start_time=timezone.now() + timedelta(days=3 + offset),
            end_time=timezone.now() + timedelta(days=3 + offset, hours=1),
            tz="UTC",
        )

    def usage_count() -> int:
        breakdown = resources.counter_for(AVAILABILITY_WINDOWS)(
            UsageContext(organization_ids=[service.organization_id])
        )
        return breakdown.get(service.organization_id, 0)

    before = usage_count()

    # Drop phys_a -- who authored the three group-scoped windows above --
    # from the roster.
    base_input.slots[0].calendar_ids = [managed_calendars["phys_b"].id]
    service.update_group(group.id, base_input)

    after = usage_count()

    assert after == before


@pytest.mark.django_db
def test_roster_change_emits_update_audit_naming_the_calendar_diff(
    service, base_input, managed_calendars, django_capture_on_commit_callbacks
):
    """`_reconcile_slot` must emit its own UPDATE audit row naming which
    calendar(s) left/arrived for the slot. The group-level audit
    `update_group` emits separately (via `_audit_group_write` on the group
    itself) carries an empty diff for a pure roster edit -- this is the only
    audit content that actually describes the roster change."""
    group = service.create_group(base_input)
    physicians = group.slots.get(name="Physicians")

    base_input.slots[0].calendar_ids = [managed_calendars["phys_b"].id]

    with patch("vinta_audit_logs.tasks.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            service.update_group(group.id, base_input)

    payloads = [call.args[0] for call in mock_task.delay.call_args_list]
    slot_updates = [
        p
        for p in payloads
        if p["action_key"] == AuditAction.UPDATE
        and p["subject"]["subject_type"] == "calendar_integration.calendargroupslot"
        and p["subject"]["subject_id"] == str(physicians.pk)
    ]
    assert len(slot_updates) == 1
    diff = slot_updates[0]["diff"]
    assert diff["calendar_ids"]["old"] == sorted(
        [managed_calendars["phys_a"].id, managed_calendars["phys_b"].id]
    )
    assert diff["calendar_ids"]["new"] == [managed_calendars["phys_b"].id]


# ---------------------------------------------------------------------------
# Fixtures -- grouped booking (create_grouped_event / reschedule_grouped_event),
# mirroring test_calendar_group_service.py's `grouped_service` + `clinic_group`.
# ---------------------------------------------------------------------------


@pytest.fixture
def internal_calendars(organization):
    calendars = {}
    for name, external in (
        ("Dr. A", "phys_a"),
        ("Dr. B", "phys_b"),
        ("Room 1", "room_1"),
    ):
        calendars[external] = Calendar.objects.create(
            organization=organization,
            name=name,
            external_id=external,
            provider=CalendarProvider.INTERNAL,
            calendar_type=(
                CalendarType.PERSONAL if external.startswith("phys_") else CalendarType.RESOURCE
            ),
            manage_available_windows=True,
            accepts_public_scheduling=True,
        )
    return calendars


@pytest.fixture
def calendar_service(organization):
    cs = CalendarService()
    cs.initialize_without_provider(organization=organization)
    return cs


def _authenticate_as_event_owner(calendar_service, event, user, organization):
    """Grant `user` an event-scoped management token with RESCHEDULE (and the
    other owner) permissions, then re-bind `calendar_service` to that user.

    Used to make `reschedule_grouped_event`'s downstream
    `CalendarService.update_event` permission check actually pass, instead of
    failing on unrelated permission plumbing -- `update_event` requires a
    `CalendarManagementToken` for `(event, user)` whenever `user_or_token` is
    a `User` (see `CalendarPermissionService.initialize_with_user`); the
    `calendar_service` fixture itself stays anonymous (`user_or_token=None`)
    so `create_grouped_event`'s group-authorized bypass keeps working
    unmodified for every other test in this module.
    """
    token = CalendarManagementToken.objects.create(
        event_fk=event,
        membership_user_id=user.id,
        token_hash=f"lenient-removal-reschedule-{event.id}",
        organization=organization,
    )
    for permission_str in DEFAULT_CALENDAR_OWNER_PERMISSIONS:
        token.permissions.create(permission=permission_str, organization_id=organization.id)
    calendar_service.initialize_without_provider(organization=organization, user_or_token=user)


@pytest.fixture
def grouped_service(organization, calendar_service, audit_service):
    svc = CalendarGroupService(
        calendar_service=calendar_service,
        audit_service=audit_service,
        calendar_permission_service=CalendarPermissionService(),
    )
    svc.initialize(organization=organization)
    return svc


@pytest.fixture
def clinic_group(grouped_service, internal_calendars):
    return grouped_service.create_group(
        CalendarGroupInputData(
            name="Clinic Appointments",
            accepts_public_scheduling=True,
            # Required since main added the public-scheduling duration invariant:
            # a group that accepts codeless public booking must carry a duration,
            # because such a booking presents no code and so inherits no per-code
            # length pin. One hour matches every booking these tests make.
            duration=timedelta(hours=1),
            slots=[
                CalendarGroupSlotInputData(
                    name="Physicians",
                    calendar_ids=[
                        internal_calendars["phys_a"].id,
                        internal_calendars["phys_b"].id,
                    ],
                    required_count=1,
                    order=0,
                ),
                CalendarGroupSlotInputData(
                    name="Rooms",
                    calendar_ids=[internal_calendars["room_1"].id],
                    required_count=1,
                    order=1,
                ),
            ],
        )
    )


def _make_window_available(calendars, start, end):
    for cal in calendars:
        AvailableTime.objects.create(
            organization=cal.organization,
            calendar=cal,
            start_time_tz_unaware=start,
            end_time_tz_unaware=end,
            timezone="UTC",
        )


def _remove_phys_a_from_roster(grouped_service, clinic_group, internal_calendars):
    """Drop phys_a from the Physicians slot's roster via update_group, leaving
    the Rooms slot untouched."""
    return grouped_service.update_group(
        clinic_group.id,
        CalendarGroupInputData(
            name=clinic_group.name,
            slots=[
                CalendarGroupSlotInputData(
                    name="Physicians",
                    calendar_ids=[internal_calendars["phys_b"].id],
                    required_count=1,
                    order=0,
                ),
                CalendarGroupSlotInputData(
                    name="Rooms",
                    calendar_ids=[internal_calendars["room_1"].id],
                    required_count=1,
                    order=1,
                ),
            ],
        ),
    )


# ---------------------------------------------------------------------------
# Selection validation: added vs. retained
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_grouped_event_still_rejects_calendar_not_in_roster(
    grouped_service, clinic_group, internal_calendars
):
    """Booking creation is unchanged: every selected calendar is an addition
    on create, so the outside-pool rejection still fires -- byte-for-byte the
    same as before this phase."""
    start = timezone.now().replace(microsecond=0) + timedelta(hours=1)
    end = start + timedelta(hours=1)
    _make_window_available(internal_calendars.values(), start, end)
    outsider = Calendar.objects.create(
        organization=grouped_service.organization,
        name="Outsider",
        external_id="outsider",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
    )
    physicians_slot = clinic_group.slots.get(name="Physicians")
    rooms_slot = clinic_group.slots.get(name="Rooms")

    with pytest.raises(CalendarGroupValidationError):
        grouped_service.create_grouped_event(
            CalendarGroupEventInputData(
                title="Bad",
                description="",
                start_time=start,
                end_time=end,
                timezone="UTC",
                group_id=clinic_group.id,
                slot_selections=[
                    CalendarGroupSlotSelectionInputData(
                        slot_id=physicians_slot.id,
                        calendar_ids=[outsider.id],
                    ),
                    CalendarGroupSlotSelectionInputData(
                        slot_id=rooms_slot.id,
                        calendar_ids=[internal_calendars["room_1"].id],
                    ),
                ],
            )
        )


# ---------------------------------------------------------------------------
# Scoped rows keep enforcing on a grandfathered booking's reschedule
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_removed_calendar_scoped_window_still_enforced_on_reschedule(
    grouped_service, admin_user, clinic_group, internal_calendars
):
    physicians_slot = clinic_group.slots.get(name="Physicians")
    rooms_slot = clinic_group.slots.get(name="Rooms")

    base_now = timezone.now().replace(microsecond=0, minute=0, second=0)
    # Broad base availability so create + both reschedule attempts pass the
    # base availability check.
    _make_window_available(internal_calendars.values(), base_now, base_now + timedelta(days=10))

    narrow_day = base_now + timedelta(days=3)
    grouped_service.create_group_scoped_availability_window(
        acting_user=admin_user,
        group_slot_id=physicians_slot.id,
        calendar_id=internal_calendars["phys_a"].id,
        start_time=narrow_day.replace(hour=9),
        end_time=narrow_day.replace(hour=17),
        tz="UTC",
    )

    start = narrow_day.replace(hour=10)
    end = narrow_day.replace(hour=11)
    event = grouped_service.create_grouped_event(
        CalendarGroupEventInputData(
            title="Follow-up",
            description="",
            start_time=start,
            end_time=end,
            timezone="UTC",
            group_id=clinic_group.id,
            slot_selections=[
                CalendarGroupSlotSelectionInputData(
                    slot_id=physicians_slot.id, calendar_ids=[internal_calendars["phys_a"].id]
                ),
                CalendarGroupSlotSelectionInputData(
                    slot_id=rooms_slot.id, calendar_ids=[internal_calendars["room_1"].id]
                ),
            ],
        )
    )

    _remove_phys_a_from_roster(grouped_service, clinic_group, internal_calendars)
    assert (
        AvailableTime.objects.unscoped()
        .filter(group_slot_fk=physicians_slot, calendar_fk=internal_calendars["phys_a"])
        .exists()
    )

    # Rescheduling to a day OUTSIDE the surviving group-scoped window is
    # still rejected, even though phys_a has left the roster.
    outside_day = narrow_day + timedelta(days=1)
    with pytest.raises(CalendarGroupScopedRuleViolationError):
        grouped_service.reschedule_grouped_event(
            event_id=event.id,
            start_time=outside_day.replace(hour=10),
            end_time=outside_day.replace(hour=11),
            tz="UTC",
        )

    # Rescheduling WITHIN the surviving window is NOT rejected. `admin_user`
    # is granted an event-scoped management token and bound to
    # `grouped_service.calendar_service` first, so the reschedule completes
    # end-to-end (unlike a bare, unauthenticated calendar_service, which
    # would raise PermissionDenied downstream and make this assertion
    # vacuous) and the outcome is checked positively.
    _authenticate_as_event_owner(
        grouped_service.calendar_service, event, admin_user, grouped_service.organization
    )
    new_start = narrow_day.replace(hour=13)
    new_end = narrow_day.replace(hour=14)
    grouped_service.reschedule_grouped_event(
        event_id=event.id,
        start_time=new_start,
        end_time=new_end,
        tz="UTC",
    )
    event.refresh_from_db()
    assert event.start_time == new_start
    assert event.end_time == new_end


@pytest.mark.django_db
def test_removed_calendar_scoped_block_still_enforced_on_reschedule(
    grouped_service, admin_user, clinic_group, internal_calendars
):
    """Clone of `test_removed_calendar_scoped_window_still_enforced_on_reschedule`
    for BlockedTime: the plan's Phase 1 test list requires all three scoped
    models proven still enforced, not merely still present."""
    physicians_slot = clinic_group.slots.get(name="Physicians")
    rooms_slot = clinic_group.slots.get(name="Rooms")

    base_now = timezone.now().replace(microsecond=0, minute=0, second=0)
    _make_window_available(internal_calendars.values(), base_now, base_now + timedelta(days=10))

    blocked_day = base_now + timedelta(days=3)
    grouped_service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=physicians_slot.id,
        calendar_id=internal_calendars["phys_a"].id,
        start_time=blocked_day.replace(hour=9),
        end_time=blocked_day.replace(hour=17),
        tz="UTC",
    )

    start = base_now + timedelta(days=1)
    start = start.replace(hour=10)
    end = start.replace(hour=11)
    event = grouped_service.create_grouped_event(
        CalendarGroupEventInputData(
            title="Follow-up",
            description="",
            start_time=start,
            end_time=end,
            timezone="UTC",
            group_id=clinic_group.id,
            slot_selections=[
                CalendarGroupSlotSelectionInputData(
                    slot_id=physicians_slot.id, calendar_ids=[internal_calendars["phys_a"].id]
                ),
                CalendarGroupSlotSelectionInputData(
                    slot_id=rooms_slot.id, calendar_ids=[internal_calendars["room_1"].id]
                ),
            ],
        )
    )

    _remove_phys_a_from_roster(grouped_service, clinic_group, internal_calendars)
    assert (
        BlockedTime.objects.unscoped()
        .filter(group_slot_fk=physicians_slot, calendar_fk=internal_calendars["phys_a"])
        .exists()
    )

    # Rescheduling into the surviving group-scoped block's span is still
    # rejected, even though phys_a has left the roster.
    with pytest.raises(CalendarGroupScopedRuleViolationError):
        grouped_service.reschedule_grouped_event(
            event_id=event.id,
            start_time=blocked_day.replace(hour=10),
            end_time=blocked_day.replace(hour=11),
            tz="UTC",
        )


@pytest.mark.django_db
def test_removed_calendar_scoped_quota_still_enforced_on_reschedule(
    grouped_service, admin_user, clinic_group, internal_calendars
):
    physicians_slot = clinic_group.slots.get(name="Physicians")
    rooms_slot = clinic_group.slots.get(name="Rooms")

    base_now = timezone.now().replace(microsecond=0, minute=0, second=0)
    _make_window_available(internal_calendars.values(), base_now, base_now + timedelta(days=10))

    grouped_service.create_group_scoped_quota_rule(
        acting_user=admin_user,
        group_slot_id=physicians_slot.id,
        calendar_id=internal_calendars["phys_a"].id,
        period=QuotaPeriod.DAY,
        cap=1,
    )

    day1 = base_now + timedelta(days=3)
    day2 = base_now + timedelta(days=4)

    # Day 1's booking is created directly against the model, not through
    # create_grouped_event: CalendarEvent.external_id is globally unique and
    # every INTERNAL-provider event created through the service gets "" (no
    # write adapter), so a second create_grouped_event call in the same test
    # would collide. The quota-count SQL function only reads
    # CalendarEventGroupSelection + CalendarEvent rows (see
    # calculate_calendar_group_quota_period_counts), so a directly-created
    # "live booking made through the group" counts identically either way.
    day1_event = CalendarEvent.objects.create(
        organization=grouped_service.organization,
        calendar_fk=internal_calendars["phys_a"],
        title="First booking",
        description="",
        external_id="ev_quota_day1",
        start_time_tz_unaware=day1.replace(hour=10),
        end_time_tz_unaware=day1.replace(hour=11),
        timezone="UTC",
        calendar_group_fk=clinic_group,
    )
    CalendarEventGroupSelection.objects.create(
        organization=grouped_service.organization,
        event=day1_event,
        slot_id=physicians_slot.id,
        calendar=internal_calendars["phys_a"],
    )  # consumes day1's cap of 1 for phys_a.

    movable_event = grouped_service.create_grouped_event(
        CalendarGroupEventInputData(
            title="Second booking",
            description="",
            start_time=day2.replace(hour=10),
            end_time=day2.replace(hour=11),
            timezone="UTC",
            group_id=clinic_group.id,
            slot_selections=[
                CalendarGroupSlotSelectionInputData(
                    slot_id=physicians_slot.id, calendar_ids=[internal_calendars["phys_a"].id]
                ),
                CalendarGroupSlotSelectionInputData(
                    slot_id=rooms_slot.id, calendar_ids=[internal_calendars["room_1"].id]
                ),
            ],
        )
    )  # under day2's own (separate) cap.

    _remove_phys_a_from_roster(grouped_service, clinic_group, internal_calendars)
    assert (
        CalendarGroupSlotQuotaRule.objects.filter_by_organization(grouped_service.organization.id)
        .filter(group_slot_fk=physicians_slot, calendar_fk=internal_calendars["phys_a"])
        .exists()
    )

    # Moving the second booking onto day1 -- already at cap for phys_a -- is
    # still rejected by the surviving quota rule.
    with pytest.raises(CalendarGroupScopedRuleViolationError):
        grouped_service.reschedule_grouped_event(
            event_id=movable_event.id,
            start_time=day1.replace(hour=14),
            end_time=day1.replace(hour=15),
            tz="UTC",
        )
