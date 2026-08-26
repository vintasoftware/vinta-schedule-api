"""Membership-removal cleanup for quota rules.

``CalendarGroupSlotQuotaRule.group_slot`` cascades (``on_delete=CASCADE``) when
the slot or its group is deleted, but NOT when a calendar is simply removed
from a slot's roster while the slot survives -- that's a
``CalendarGroupSlotMembership`` deletion, which the FK doesn't observe.
``CalendarGroupService._reconcile_slot`` (mirroring the pattern already
established for group-scoped windows and blocked time) explicitly deletes
orphaned quota rules for removed calendars.

There is no write-service method for quota rules yet -- rules are created
directly through the model/factory here, the same way group-scoped
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
from calendar_integration.factories import create_group_slot_quota_rule
from calendar_integration.models import (
    Calendar,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarGroupSlotQuotaRule,
)
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.dataclasses import (
    CalendarGroupInputData,
    CalendarGroupSlotInputData,
)
from organizations.models import Organization


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Quota Reconcile Org", should_sync_rooms=False)


@pytest.fixture
def audit_service() -> OrganizationAuditService:
    from di_core.containers import container

    return container.audit_service()


@pytest.fixture
def service(
    organization: Organization, audit_service: OrganizationAuditService
) -> CalendarGroupService:
    svc = CalendarGroupService(audit_service=audit_service)
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
def group(organization: Organization) -> CalendarGroup:
    return CalendarGroup.objects.create(organization=organization, name="Surgery")


@pytest.fixture
def group_slot(
    organization: Organization,
    group: CalendarGroup,
    calendar: Calendar,
    other_calendar: Calendar,
) -> CalendarGroupSlot:
    slot = CalendarGroupSlot.objects.create(organization=organization, group=group, name="Lead")
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=calendar
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=other_calendar
    )
    return slot


@pytest.mark.django_db
def test_removing_calendar_from_slot_removes_quota_rules(
    service: CalendarGroupService,
    organization: Organization,
    calendar: Calendar,
    other_calendar: Calendar,
    group: CalendarGroup,
    group_slot: CalendarGroupSlot,
    django_capture_on_commit_callbacks,
) -> None:
    """Removing one calendar from the slot's roster (via update_group ->
    _reconcile_slot) deletes ONLY that calendar's quota rules; the second
    calendar's rule for the same slot survives, and the deletion is audited.
    """
    rule1 = create_group_slot_quota_rule(
        organization=organization,
        group_slot=group_slot,
        calendar=calendar,
        period=QuotaPeriod.WEEK,
        cap=3,
    )
    rule2 = create_group_slot_quota_rule(
        organization=organization,
        group_slot=group_slot,
        calendar=other_calendar,
        period=QuotaPeriod.WEEK,
        cap=3,
    )

    assert (
        CalendarGroupSlotQuotaRule.objects.filter_by_organization(organization.id)
        .filter(id=rule1.id)
        .exists()
    )
    assert (
        CalendarGroupSlotQuotaRule.objects.filter_by_organization(organization.id)
        .filter(id=rule2.id)
        .exists()
    )

    with patch("vinta_audit_logs.tasks.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            service.update_group(
                group.id,
                CalendarGroupInputData(
                    name=group.name,
                    slots=[
                        CalendarGroupSlotInputData(
                            name=group_slot.name,
                            calendar_ids=[other_calendar.id],
                            required_count=1,
                        )
                    ],
                ),
            )

    # calendar's rule is gone...
    assert (
        not CalendarGroupSlotQuotaRule.objects.filter_by_organization(organization.id)
        .filter(id=rule1.id)
        .exists()
    )
    # ...other_calendar's rule for the same slot survives.
    assert (
        CalendarGroupSlotQuotaRule.objects.filter_by_organization(organization.id)
        .filter(id=rule2.id)
        .exists()
    )

    payloads = [call.args[0] for call in mock_task.delay.call_args_list]
    quota_rule_delete_payloads = [
        p
        for p in payloads
        if p["action_key"] == AuditAction.DELETE
        and p["subject"]["subject_type"] == "calendar_integration.calendargroupslotquotarule"
    ]
    assert len(quota_rule_delete_payloads) == 1
    assert quota_rule_delete_payloads[0]["subject"]["subject_id"] == str(rule1.id)


@pytest.mark.django_db
def test_deleting_slot_through_update_group_cascades_quota_rules(
    service: CalendarGroupService,
    organization: Organization,
    calendar: Calendar,
    group: CalendarGroup,
    group_slot: CalendarGroupSlot,
) -> None:
    """Deleting the slot entirely (schema-enforced CASCADE, not the explicit
    membership-removal cleanup) also removes its quota rules."""
    rule = create_group_slot_quota_rule(
        organization=organization, group_slot=group_slot, calendar=calendar
    )

    service.update_group(group.id, CalendarGroupInputData(name=group.name, slots=[]))

    assert (
        not CalendarGroupSlot.objects.filter_by_organization(organization.id)
        .filter(id=group_slot.id)
        .exists()
    )
    assert (
        not CalendarGroupSlotQuotaRule.objects.filter_by_organization(organization.id)
        .filter(id=rule.id)
        .exists()
    )
