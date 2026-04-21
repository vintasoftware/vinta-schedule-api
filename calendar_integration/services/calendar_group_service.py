import datetime
from collections.abc import Iterable
from typing import Annotated

from django.db import transaction
from django.utils import timezone

from dependency_injector.wiring import Provide, inject

from calendar_integration.exceptions import (
    CalendarGroupHasFutureEventsError,
    CalendarGroupSlotInUseError,
    CalendarGroupValidationError,
    CalendarServiceOrganizationNotSetError,
)
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
)
from calendar_integration.querysets import CalendarEventQuerySet
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.dataclasses import (
    BookableSlotProposal,
    CalendarGroupInputData,
    CalendarGroupRangeAvailability,
    CalendarGroupSlotAvailability,
    CalendarGroupSlotInputData,
)
from organizations.models import Organization


class CalendarGroupService:
    organization: Organization | None

    @inject
    def __init__(
        self,
        calendar_permission_service: Annotated[
            "CalendarPermissionService | None", Provide["calendar_permission_service"]
        ] = None,
    ) -> None:
        self.organization = None
        self.calendar_permission_service = calendar_permission_service

    def initialize(self, organization: Organization) -> None:
        self.organization = organization

    def _assert_initialized(self) -> None:
        if self.organization is None:
            raise CalendarServiceOrganizationNotSetError(
                "CalendarGroupService requires an organization. Call initialize()."
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _get_group_by_id(self, group_id: int) -> CalendarGroup:
        self._assert_initialized()
        return CalendarGroup.objects.filter_by_organization(self.organization.id).get(id=group_id)

    def _validate_slots_input(
        self, slots: Iterable[CalendarGroupSlotInputData]
    ) -> tuple[list[CalendarGroupSlotInputData], set[int]]:
        slots = list(slots)

        seen_slot_names: set[str] = set()
        for slot_data in slots:
            if slot_data.name in seen_slot_names:
                raise CalendarGroupValidationError(f"Duplicate slot name: {slot_data.name!r}.")
            seen_slot_names.add(slot_data.name)

            if not slot_data.calendar_ids:
                raise CalendarGroupValidationError(
                    f"Slot {slot_data.name!r} must include at least one calendar."
                )
            if len(set(slot_data.calendar_ids)) != len(slot_data.calendar_ids):
                raise CalendarGroupValidationError(
                    f"Slot {slot_data.name!r} contains duplicate calendars."
                )
            if slot_data.required_count < 1:
                raise CalendarGroupValidationError(
                    f"Slot {slot_data.name!r} required_count must be >= 1."
                )
            if slot_data.required_count > len(slot_data.calendar_ids):
                raise CalendarGroupValidationError(
                    f"Slot {slot_data.name!r} required_count ({slot_data.required_count}) "
                    f"exceeds pool size ({len(slot_data.calendar_ids)})."
                )

        all_calendar_ids = {cid for slot in slots for cid in slot.calendar_ids}
        if all_calendar_ids:
            org_calendar_ids = set(
                Calendar.objects.filter_by_organization(self.organization.id)
                .filter(id__in=all_calendar_ids)
                .values_list("id", flat=True)
            )
            missing = all_calendar_ids - org_calendar_ids
            if missing:
                raise CalendarGroupValidationError(
                    f"Calendars {sorted(missing)} do not belong to this organization."
                )
        return slots, all_calendar_ids

    def _ensure_no_future_selections(
        self,
        slot: CalendarGroupSlot,
        calendar_ids: Iterable[int] | None = None,
    ) -> None:
        """Raise if a CalendarEventGroupSelection points at `slot` (optionally filtered
        by `calendar_ids`) for an event that starts in the future."""
        now = timezone.now()
        qs = CalendarEventGroupSelection.objects.filter_by_organization(
            self.organization.id
        ).filter(slot_fk=slot, event_fk__start_time__gt=now)
        if calendar_ids is not None:
            qs = qs.filter(calendar_fk_id__in=list(calendar_ids))
        if qs.exists():
            raise CalendarGroupSlotInUseError()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @transaction.atomic()
    def create_group(self, data: CalendarGroupInputData) -> CalendarGroup:
        """Create a CalendarGroup with its slots and memberships."""
        self._assert_initialized()
        slots_data, _ = self._validate_slots_input(data.slots)

        group = CalendarGroup.objects.create(
            organization=self.organization,
            name=data.name,
            description=data.description,
        )
        self._create_slots(group, slots_data)
        return group

    @transaction.atomic()
    def update_group(self, group_id: int, data: CalendarGroupInputData) -> CalendarGroup:
        """Reconcile a CalendarGroup's slots and memberships with `data`.

        Slots are matched by name. Removing a slot, or removing a calendar from an
        existing slot's pool, is refused if any future-booked event references it.
        """
        self._assert_initialized()
        group = self._get_group_by_id(group_id)
        slots_data, _ = self._validate_slots_input(data.slots)

        group.name = data.name
        group.description = data.description
        group.save(update_fields=["name", "description", "modified"])

        existing_slots = {s.name: s for s in group.slots.all()}
        incoming_names = {s.name for s in slots_data}

        for name, slot in existing_slots.items():
            if name not in incoming_names:
                self._ensure_no_future_selections(slot=slot)
                slot.delete()

        for slot_data in slots_data:
            if slot_data.name in existing_slots:
                self._reconcile_slot(existing_slots[slot_data.name], slot_data)
            else:
                self._create_slots(group, [slot_data])

        return group

    @transaction.atomic()
    def delete_group(self, group_id: int) -> None:
        """Delete a CalendarGroup. Refuses if any events (past or future) reference
        it, matching the PROTECT FK on `CalendarEvent.calendar_group`."""
        self._assert_initialized()
        group = self._get_group_by_id(group_id)

        if (
            CalendarEvent.objects.filter_by_organization(self.organization.id)
            .filter(calendar_group_fk=group)
            .exists()
        ):
            raise CalendarGroupHasFutureEventsError(
                "Cannot delete CalendarGroup because it has bookings."
            )

        group.delete()

    def _create_slots(
        self,
        group: CalendarGroup,
        slots_data: Iterable[CalendarGroupSlotInputData],
    ) -> None:
        for slot_data in slots_data:
            slot = CalendarGroupSlot.objects.create(
                organization=self.organization,
                group=group,
                name=slot_data.name,
                description=slot_data.description,
                order=slot_data.order,
                required_count=slot_data.required_count,
            )
            CalendarGroupSlotMembership.objects.bulk_create(
                [
                    CalendarGroupSlotMembership(
                        organization=self.organization,
                        slot_fk=slot,
                        calendar_fk_id=cid,
                    )
                    for cid in slot_data.calendar_ids
                ]
            )

    def _reconcile_slot(
        self,
        slot: CalendarGroupSlot,
        slot_data: CalendarGroupSlotInputData,
    ) -> None:
        slot.description = slot_data.description
        slot.order = slot_data.order
        slot.required_count = slot_data.required_count
        slot.save(update_fields=["description", "order", "required_count", "modified"])

        existing_calendar_ids = set(slot.memberships.values_list("calendar_fk_id", flat=True))
        incoming_calendar_ids = set(slot_data.calendar_ids)

        to_remove = existing_calendar_ids - incoming_calendar_ids
        to_add = incoming_calendar_ids - existing_calendar_ids

        if to_remove:
            self._ensure_no_future_selections(slot=slot, calendar_ids=to_remove)
            CalendarGroupSlotMembership.objects.filter_by_organization(self.organization.id).filter(
                slot_fk=slot, calendar_fk_id__in=to_remove
            ).delete()

        if to_add:
            CalendarGroupSlotMembership.objects.bulk_create(
                [
                    CalendarGroupSlotMembership(
                        organization=self.organization,
                        slot_fk=slot,
                        calendar_fk_id=cid,
                    )
                    for cid in to_add
                ]
            )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def get_group_events(
        self,
        group_id: int,
        start: datetime.datetime,
        end: datetime.datetime,
    ) -> CalendarEventQuerySet:
        """Return the events booked under a group that overlap [start, end].

        Recurring occurrences are annotated onto each master event via
        `annotate_recurring_occurrences_on_date_range`; callers can expand them
        through the recurring-mixin helpers on individual events.
        """
        self._assert_initialized()
        group = self._get_group_by_id(group_id)

        return (
            CalendarEvent.objects.filter_by_organization(self.organization.id)
            .annotate_recurring_occurrences_on_date_range(start, end)
            .filter(
                calendar_group_fk=group,
                start_time__lt=end,
                end_time__gt=start,
            )
        )

    def check_group_availability(
        self,
        group_id: int,
        ranges: Iterable[tuple[datetime.datetime, datetime.datetime]],
    ) -> list[CalendarGroupRangeAvailability]:
        """For every range, list which calendars in each slot's pool are available.

        A slot with an empty `available_calendar_ids` is unbookable for that range.
        """
        self._assert_initialized()
        group = self._get_group_by_id(group_id)
        ranges = list(ranges)

        slots = list(group.slots.all())
        slot_pool_by_id: dict[int, set[int]] = {
            s.id: set(
                CalendarGroupSlotMembership.objects.filter_by_organization(self.organization.id)
                .filter(slot_fk=s)
                .values_list("calendar_fk_id", flat=True)
            )
            for s in slots
        }

        results: list[CalendarGroupRangeAvailability] = []
        for start, end in ranges:
            available_ids = set(
                Calendar.objects.filter_by_organization(self.organization.id)
                .only_calendars_available_in_ranges([(start, end)])
                .values_list("id", flat=True)
            )
            slot_results = [
                CalendarGroupSlotAvailability(
                    slot_id=s.id,
                    available_calendar_ids=sorted(slot_pool_by_id[s.id] & available_ids),
                )
                for s in slots
            ]
            results.append(
                CalendarGroupRangeAvailability(
                    start_time=start,
                    end_time=end,
                    slots=slot_results,
                )
            )
        return results

    def find_bookable_slots(
        self,
        group_id: int,
        search_window_start: datetime.datetime,
        search_window_end: datetime.datetime,
        duration: datetime.timedelta,
        slot_step: datetime.timedelta = datetime.timedelta(minutes=15),
    ) -> list[BookableSlotProposal]:
        """Walk `[search_window_start, search_window_end]` in `slot_step` increments
        and return the start/end pairs where every slot of the group is satisfied.

        v1 scans in Python, firing one availability query per step; we leave SQL-side
        window generation for a follow-up (see plan PR5).
        """
        self._assert_initialized()
        if slot_step <= datetime.timedelta(0):
            raise CalendarGroupValidationError("slot_step must be a positive timedelta.")
        if duration <= datetime.timedelta(0):
            raise CalendarGroupValidationError("duration must be a positive timedelta.")

        group = self._get_group_by_id(group_id)

        proposals: list[BookableSlotProposal] = []
        cursor = search_window_start
        while cursor + duration <= search_window_end:
            window_start = cursor
            window_end = cursor + duration
            is_bookable = (
                CalendarGroup.objects.filter_by_organization(self.organization.id)
                .filter(id=group.id)
                .only_groups_bookable_in_ranges([(window_start, window_end)])
                .exists()
            )
            if is_bookable:
                proposals.append(BookableSlotProposal(start_time=window_start, end_time=window_end))
            cursor = cursor + slot_step
        return proposals
