"""Membership-removal survival for quota rules.

``AppointmentTypeSlotQuotaRule.appointment_type_slot`` cascades (``on_delete=CASCADE``) when
the slot or its appointment type is deleted, but NOT when a calendar is simply removed
from a slot's roster while the slot survives -- that's a
``AppointmentTypeSlotMembership`` deletion, which the FK doesn't observe.
``AppointmentTypeService._reconcile_slot`` deletes only the membership row in
that case (Calendar Pools Phase 1: roster removal is lenient and never
destroys configuration) -- the departed calendar's quota rules for that slot
are kept and keep enforcing.

There is no write-service method for quota rules yet -- rules are created
directly through the model/factory here, the same way appointment-type-scoped
``AvailableTime``/``BlockedTime`` rows were once inserted directly before any
write service existed for them.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from audit_integration.constants import AuditAction
from audit_integration.services import OrganizationAuditService
from calendar_integration.constants import CalendarProvider, CalendarType, QuotaPeriod
from calendar_integration.factories import create_appointment_type_slot_quota_rule
from calendar_integration.models import (
    AppointmentType,
    AppointmentTypeSlot,
    AppointmentTypeSlotMembership,
    AppointmentTypeSlotQuotaRule,
    Calendar,
)
from calendar_integration.services.appointment_type_service import AppointmentTypeService
from calendar_integration.services.dataclasses import (
    AppointmentTypeInputData,
    AppointmentTypeSlotInputData,
)
from organizations.models import Organization


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Quota Reconcile Org", should_sync_rooms=False)


@pytest.fixture
def audit_service() -> OrganizationAuditService:
    from di_core.containers import get_container

    return get_container().audit_service()


@pytest.fixture
def service(
    organization: Organization, audit_service: OrganizationAuditService
) -> AppointmentTypeService:
    svc = AppointmentTypeService(audit_service=audit_service)
    svc.initialize(organization=organization)
    return svc


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        organization=organization,
        name="Dr. Reyes",
        external_id="quota-reconcile-cal",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
    )


@pytest.fixture
def other_calendar(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        organization=organization,
        name="Dr. Costa",
        external_id="quota-reconcile-other-cal",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
    )


@pytest.fixture
def appointment_type(organization: Organization) -> AppointmentType:
    return AppointmentType.objects.create(organization=organization, name="Surgery")


@pytest.fixture
def appointment_type_slot(
    organization: Organization,
    appointment_type: AppointmentType,
    calendar: Calendar,
    other_calendar: Calendar,
) -> AppointmentTypeSlot:
    slot = AppointmentTypeSlot.objects.create(
        organization=organization, appointment_type=appointment_type, name="Lead"
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=calendar
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=other_calendar
    )
    return slot


@pytest.mark.django_db
def test_removing_calendar_from_slot_keeps_quota_rules(
    service: AppointmentTypeService,
    organization: Organization,
    calendar: Calendar,
    other_calendar: Calendar,
    appointment_type: AppointmentType,
    appointment_type_slot: AppointmentTypeSlot,
    django_capture_on_commit_callbacks,
) -> None:
    """Contract change (Calendar Pools Phase 1): removing one calendar from the
    slot's roster (via update_appointment_type -> _reconcile_slot) deletes ONLY the
    AppointmentTypeSlotMembership row. Both quota rules survive -- the departed
    calendar's rule is kept (and no longer audited as a delete), and the other
    calendar's rule for the same slot is untouched either way."""
    rule1 = create_appointment_type_slot_quota_rule(
        organization=organization,
        appointment_type_slot=appointment_type_slot,
        calendar=calendar,
        period=QuotaPeriod.WEEK,
        cap=3,
    )
    rule2 = create_appointment_type_slot_quota_rule(
        organization=organization,
        appointment_type_slot=appointment_type_slot,
        calendar=other_calendar,
        period=QuotaPeriod.WEEK,
        cap=3,
    )

    assert (
        AppointmentTypeSlotQuotaRule.objects.filter_by_organization(organization.id)
        .filter(id=rule1.id)
        .exists()
    )
    assert (
        AppointmentTypeSlotQuotaRule.objects.filter_by_organization(organization.id)
        .filter(id=rule2.id)
        .exists()
    )

    with patch("vinta_audit_logs.tasks.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            service.update_appointment_type(
                appointment_type.id,
                AppointmentTypeInputData(
                    name=appointment_type.name,
                    slots=[
                        AppointmentTypeSlotInputData(
                            name=appointment_type_slot.name,
                            calendar_ids=[other_calendar.id],
                            required_count=1,
                        )
                    ],
                ),
            )

    # Both rules survive the roster removal ...
    assert (
        AppointmentTypeSlotQuotaRule.objects.filter_by_organization(organization.id)
        .filter(id=rule1.id)
        .exists()
    )
    assert (
        AppointmentTypeSlotQuotaRule.objects.filter_by_organization(organization.id)
        .filter(id=rule2.id)
        .exists()
    )
    # ... the calendar's membership is what actually got removed.
    assert (
        not AppointmentTypeSlotMembership.objects.filter_by_organization(organization.id)
        .filter(slot_fk=appointment_type_slot, calendar_fk_id=calendar.id)
        .exists()
    )

    # No quota-rule DELETE is audited -- nothing was deleted.
    payloads = [call.args[0] for call in mock_task.delay.call_args_list]
    quota_rule_delete_payloads = [
        p
        for p in payloads
        if p["action_key"] == AuditAction.DELETE
        and p["subject"]["subject_type"] == "calendar_integration.appointmenttypeslotquotarule"
    ]
    assert quota_rule_delete_payloads == []


@pytest.mark.django_db
def test_deleting_slot_through_update_appointment_type_cascades_quota_rules(
    service: AppointmentTypeService,
    organization: Organization,
    calendar: Calendar,
    appointment_type: AppointmentType,
    appointment_type_slot: AppointmentTypeSlot,
) -> None:
    """Deleting the slot entirely (schema-enforced CASCADE, not the explicit
    membership-removal cleanup) also removes its quota rules."""
    rule = create_appointment_type_slot_quota_rule(
        organization=organization, appointment_type_slot=appointment_type_slot, calendar=calendar
    )

    service.update_appointment_type(
        appointment_type.id, AppointmentTypeInputData(name=appointment_type.name, slots=[])
    )

    assert (
        not AppointmentTypeSlot.objects.filter_by_organization(organization.id)
        .filter(id=appointment_type_slot.id)
        .exists()
    )
    assert (
        not AppointmentTypeSlotQuotaRule.objects.filter_by_organization(organization.id)
        .filter(id=rule.id)
        .exists()
    )
