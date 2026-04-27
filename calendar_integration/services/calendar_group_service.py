import datetime
from collections.abc import Iterable
from typing import TYPE_CHECKING, Annotated

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from dependency_injector.wiring import Provide, inject

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.exceptions import (
    CalendarGroupHasFutureEventsError,
    CalendarGroupSlotInUseError,
    CalendarGroupValidationError,
    CalendarServiceOrganizationNotSetError,
)
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
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
    CalendarEventInputData,
    CalendarGroupEventInputData,
    CalendarGroupInputData,
    CalendarGroupRangeAvailability,
    CalendarGroupSlotAvailability,
    CalendarGroupSlotInputData,
    EventAttendanceInputData,
)
from organizations.models import Organization
from users.models import User


if TYPE_CHECKING:
    from calendar_integration.services.calendar_service import CalendarService


def _intervals_overlap(
    a: tuple[datetime.datetime, datetime.datetime],
    b: tuple[datetime.datetime, datetime.datetime],
) -> bool:
    a_start, a_end = a
    b_start, b_end = b
    return a_start < b_end and b_start < a_end


class CalendarGroupService:
    organization: Organization | None

    @inject
    def __init__(
        self,
        calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
        calendar_permission_service: Annotated[
            "CalendarPermissionService | None", Provide["calendar_permission_service"]
        ] = None,
    ) -> None:
        self.organization = None
        self.calendar_service = calendar_service
        self.calendar_permission_service = calendar_permission_service

    def initialize(self, organization: Organization) -> None:
        """Initialize the service with the tenant organization.

        For methods that need to delegate event creation to `CalendarService`
        (i.e. `create_grouped_event`), the caller must also separately initialize
        or authenticate `self.calendar_service` with the same organization — the
        grouped-event flow needs external-provider adapters if any of the
        selected calendars is backed by one.
        """
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
        with_bulk_modifications: bool = False,
    ) -> list[CalendarGroupRangeAvailability]:
        """For every range, list which calendars in each slot's pool are available.

        A slot with an empty `available_calendar_ids` is unbookable for that range.
        Set `with_bulk_modifications=True` to expand recurring events through
        their bulk-modification continuation series.
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

        calendar_qs_method = (
            "only_calendars_available_in_ranges_with_bulk_modifications"
            if with_bulk_modifications
            else "only_calendars_available_in_ranges"
        )

        results: list[CalendarGroupRangeAvailability] = []
        for start, end in ranges:
            available_ids = set(
                getattr(
                    Calendar.objects.filter_by_organization(self.organization.id),
                    calendar_qs_method,
                )([(start, end)]).values_list("id", flat=True)
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

    # ------------------------------------------------------------------
    # Grouped event creation
    # ------------------------------------------------------------------
    @transaction.atomic()
    def create_grouped_event(self, data: CalendarGroupEventInputData) -> CalendarEvent:
        """Create an event booked through a CalendarGroup.

        Persistence strategy (per the plan — "Option B"): the event is created
        on the primary calendar via `CalendarService.create_event` so existing
        side-effects, permissions, and external-provider sync run unchanged.
        Non-primary selected calendars get `BlockedTime` rows so they appear as
        busy. A `CalendarEventGroupSelection` row is written for every
        (slot, calendar) pick.

        The primary calendar is the first `calendar_id` listed in the
        lowest-`order` slot of the group.

        Preconditions:
          - `self.calendar_service` is set and initialized/authenticated for
            the same organization. The caller owns that setup because the
            primary calendar's provider dictates which flavor of CalendarService
            init is appropriate (authenticate vs initialize_without_provider).
        """
        self._assert_initialized()
        if self.calendar_service is None:
            raise CalendarGroupValidationError(
                "CalendarGroupService.calendar_service must be provided to create grouped events."
            )
        if self.calendar_service.organization is None:
            raise CalendarGroupValidationError(
                "The injected CalendarService is not initialized with an organization."
            )
        if self.calendar_service.organization.id != self.organization.id:
            raise CalendarGroupValidationError(
                "The injected CalendarService is initialized with a different organization."
            )

        group = self._get_group_by_id(data.group_id)
        slots = list(group.slots.order_by("order", "id"))
        if not slots:
            raise CalendarGroupValidationError("CalendarGroup has no slots to satisfy.")

        selections_by_slot_id = self._validate_selections(group, slots, data.slot_selections)
        all_selected_ids = {cid for sel in data.slot_selections for cid in sel.calendar_ids}
        self._assert_calendars_available(all_selected_ids, data.start_time, data.end_time)

        primary_slot = slots[0]
        primary_calendar_id = selections_by_slot_id[primary_slot.id].calendar_ids[0]

        selected_calendars = {
            c.id: c
            for c in Calendar.objects.filter_by_organization(self.organization.id).filter(
                id__in=all_selected_ids
            )
        }
        primary_calendar = selected_calendars[primary_calendar_id]

        owners_by_calendar_id = self._collect_owners_by_calendar(all_selected_ids)
        merged_attendances = self._merge_attendances(
            explicit=data.attendances, owners_by_calendar_id=owners_by_calendar_id
        )

        event_input = CalendarEventInputData(
            title=data.title,
            description=data.description,
            start_time=data.start_time,
            end_time=data.end_time,
            timezone=data.timezone,
            attendances=merged_attendances,
            external_attendances=list(data.external_attendances),
        )
        event = self.calendar_service.create_event(
            calendar_id=primary_calendar_id, event_data=event_input
        )

        event.calendar_group_fk = group
        event.save(update_fields=["calendar_group_fk"])

        CalendarEventGroupSelection.objects.bulk_create(
            [
                CalendarEventGroupSelection(
                    organization=self.organization,
                    event_fk=event,
                    slot_fk_id=sel.slot_id,
                    calendar_fk_id=cid,
                )
                for sel in data.slot_selections
                for cid in sel.calendar_ids
            ]
        )

        self._create_non_primary_blocked_times(
            event=event,
            primary_calendar=primary_calendar,
            selected_calendars=selected_calendars,
            owners_by_calendar_id=owners_by_calendar_id,
            start_time=data.start_time,
            end_time=data.end_time,
            tz=data.timezone,
        )

        return event

    def _validate_selections(
        self,
        group: CalendarGroup,
        slots: list[CalendarGroupSlot],
        selections,
    ) -> dict[int, "object"]:
        slot_by_id = {s.id: s for s in slots}

        seen_slot_ids: set[int] = set()
        selections_by_slot_id: dict[int, object] = {}
        for sel in selections:
            if sel.slot_id in seen_slot_ids:
                raise CalendarGroupValidationError(
                    f"Duplicate slot_id {sel.slot_id} in slot_selections."
                )
            seen_slot_ids.add(sel.slot_id)
            if sel.slot_id not in slot_by_id:
                raise CalendarGroupValidationError(
                    f"slot_id {sel.slot_id} does not belong to group {group.id}."
                )
            if not sel.calendar_ids:
                raise CalendarGroupValidationError(
                    f"Selection for slot {sel.slot_id} has no calendars."
                )
            if len(set(sel.calendar_ids)) != len(sel.calendar_ids):
                raise CalendarGroupValidationError(
                    f"Selection for slot {sel.slot_id} contains duplicate calendars."
                )
            selections_by_slot_id[sel.slot_id] = sel

        # Every slot must be covered with >= required_count picks, all from its pool.
        for slot in slots:
            sel = selections_by_slot_id.get(slot.id)
            if sel is None:
                raise CalendarGroupValidationError(f"Slot {slot.name!r} has no selection.")
            if len(sel.calendar_ids) < slot.required_count:
                raise CalendarGroupValidationError(
                    f"Slot {slot.name!r} requires {slot.required_count} calendar(s); "
                    f"got {len(sel.calendar_ids)}."
                )
            pool = set(slot.memberships.values_list("calendar_fk_id", flat=True))
            outside_pool = set(sel.calendar_ids) - pool
            if outside_pool:
                raise CalendarGroupValidationError(
                    f"Calendars {sorted(outside_pool)} are not in the pool of "
                    f"slot {slot.name!r}."
                )
        return selections_by_slot_id

    def _collect_owners_by_calendar(
        self, selected_calendar_ids: Iterable[int]
    ) -> dict[int, set[int]]:
        """Map each selected calendar's id → set of owner user ids."""
        selected_calendar_ids = list(selected_calendar_ids)
        if not selected_calendar_ids:
            return {}
        rows = User.objects.filter(
            calendar_ownerships__calendar_fk_id__in=selected_calendar_ids,
            calendar_ownerships__organization_id=self.organization.id,
        ).values_list("calendar_ownerships__calendar_fk_id", "id")
        owners_by_calendar: dict[int, set[int]] = {}
        for cal_id, user_id in rows:
            owners_by_calendar.setdefault(cal_id, set()).add(user_id)
        return owners_by_calendar

    def _merge_attendances(
        self,
        explicit: Iterable[EventAttendanceInputData],
        owners_by_calendar_id: dict[int, set[int]],
    ) -> list[EventAttendanceInputData]:
        """Return `explicit` attendances plus one entry per owner of every
        selected calendar. Mirrors the bundle-event behavior so non-primary
        physicians (etc.) get invited to the primary event and see it in their
        own provider calendar, rather than only observing a local BlockedTime.

        Resource calendars typically have no owners, so they contribute nothing
        here — deciding whether to attach them via `resource_allocations` is out
        of scope for this PR.
        """
        user_ids: set[int] = {a.user_id for a in explicit}
        merged = list(explicit)
        for owners in owners_by_calendar_id.values():
            for user_id in owners:
                if user_id in user_ids:
                    continue
                user_ids.add(user_id)
                merged.append(EventAttendanceInputData(user_id=user_id))
        return merged

    def _assert_calendars_available(
        self,
        calendar_ids: Iterable[int],
        start: datetime.datetime,
        end: datetime.datetime,
    ) -> None:
        calendar_ids = set(calendar_ids)
        if not calendar_ids:
            return
        available_ids = set(
            Calendar.objects.filter_by_organization(self.organization.id)
            .filter(id__in=calendar_ids)
            .only_calendars_available_in_ranges([(start, end)])
            .values_list("id", flat=True)
        )
        unavailable = calendar_ids - available_ids
        if unavailable:
            raise CalendarGroupValidationError(
                f"Selected calendars {sorted(unavailable)} are not available for "
                f"the requested time window."
            )

    def _create_non_primary_blocked_times(
        self,
        event: CalendarEvent,
        primary_calendar: Calendar,
        selected_calendars: dict[int, Calendar],
        owners_by_calendar_id: dict[int, set[int]],
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        tz: str,
    ) -> None:
        """Create a BlockedTime on every non-primary selected calendar *unless*
        the external-provider invite sync will reliably produce an equivalent
        CalendarEvent on that calendar.

        The skip only applies when we can be confident the sync will land:
          - The primary and non-primary calendars use the **same** external
            provider (e.g. Google→Google or Microsoft→Microsoft). Same-provider
            invites sync natively through the provider graph.
          - That provider is not INTERNAL (INTERNAL events don't leave the app).
          - The non-primary calendar has an owner who ends up on the attendee
            list, so the provider actually has someone to deliver the event to.

        Everything else — resource calendars, ownerless calendars, INTERNAL
        calendars, and **cross-provider** pairings (Google↔Microsoft) — gets a
        local BlockedTime. Cross-provider invites rely on email/iCalendar and
        whether the recipient's mailbox happens to be wired into their calendar
        client; we don't trust that enough to drop the local busy marker.
        """
        non_primary_ids = set(selected_calendars.keys()) - {primary_calendar.id}
        if not non_primary_ids:
            return

        primary_provider = primary_calendar.provider
        primary_can_send_invites = primary_provider != CalendarProvider.INTERNAL

        for cid in non_primary_ids:
            calendar = selected_calendars[cid]
            if calendar.calendar_type == CalendarType.BUNDLE:
                raise CalendarGroupValidationError(
                    "Bundle calendars cannot be selected for grouped events."
                )

            invite_will_sync_event = (
                primary_can_send_invites
                and calendar.provider == primary_provider
                and bool(owners_by_calendar_id.get(cid))
            )
            if invite_will_sync_event:
                # The provider will create the event on this calendar; a local
                # BlockedTime would be a duplicate.
                continue

            BlockedTime.objects.create(
                organization=self.organization,
                calendar=calendar,
                start_time_tz_unaware=start_time,
                end_time_tz_unaware=end_time,
                timezone=tz,
                reason=f"Group booking: {event.title}",
                external_id=f"group-event-{event.id}-cal-{cid}",
            )

    def find_bookable_slots(
        self,
        group_id: int,
        search_window_start: datetime.datetime,
        search_window_end: datetime.datetime,
        duration: datetime.timedelta,
        slot_step: datetime.timedelta = datetime.timedelta(minutes=15),
        with_bulk_modifications: bool = False,
    ) -> list[BookableSlotProposal]:
        """Return every `(candidate_start, candidate_start + duration)` within
        `[search_window_start, search_window_end]`, stepping by `slot_step`,
        where every slot in the group has at least `required_count` calendars
        available.

        The implementation fetches blocking data (AvailableTime for managed
        calendars, CalendarEvent + BlockedTime for unmanaged calendars) once
        for the whole search window and then walks candidates in Python — one
        query per type instead of one query per candidate. For a 24h window at
        15-minute steps that turns 96 round-trips into 3, which is the core of
        the "SQL generate_series" optimization the plan called for.

        Set `with_bulk_modifications=True` to expand recurring events through
        their bulk-modification continuation series.
        """
        self._assert_initialized()
        if slot_step <= datetime.timedelta(0):
            raise CalendarGroupValidationError("slot_step must be a positive timedelta.")
        if duration <= datetime.timedelta(0):
            raise CalendarGroupValidationError("duration must be a positive timedelta.")

        group = self._get_group_by_id(group_id)
        slots = list(group.slots.all())
        if not slots:
            return []

        slot_pool_by_id: dict[int, set[int]] = {
            s.id: set(
                CalendarGroupSlotMembership.objects.filter_by_organization(self.organization.id)
                .filter(slot_fk=s)
                .values_list("calendar_fk_id", flat=True)
            )
            for s in slots
        }
        required_count_by_slot_id = {s.id: s.required_count for s in slots}

        all_calendar_ids: set[int] = set()
        for ids in slot_pool_by_id.values():
            all_calendar_ids.update(ids)
        if not all_calendar_ids:
            return []

        managed_ids, unmanaged_ids = self._split_calendars_by_management(all_calendar_ids)
        available_spans = self._fetch_available_spans(
            managed_ids, search_window_start, search_window_end
        )
        blocking_spans = self._fetch_blocking_spans(
            unmanaged_ids,
            search_window_start,
            search_window_end,
            with_bulk_modifications=with_bulk_modifications,
        )

        proposals: list[BookableSlotProposal] = []
        cursor = search_window_start
        while cursor + duration <= search_window_end:
            window_start = cursor
            window_end = cursor + duration

            all_slots_satisfied = True
            for slot_id, pool_ids in slot_pool_by_id.items():
                available_count = 0
                for cid in pool_ids:
                    if cid in managed_ids:
                        # Managed: needs an AvailableTime that covers the window.
                        if any(
                            av_start <= window_start and av_end >= window_end
                            for av_start, av_end in available_spans.get(cid, ())
                        ):
                            available_count += 1
                    else:
                        # Unmanaged: must not overlap any blocking span.
                        if not any(
                            _intervals_overlap((bs, be), (window_start, window_end))
                            for bs, be in blocking_spans.get(cid, ())
                        ):
                            available_count += 1
                if available_count < required_count_by_slot_id[slot_id]:
                    all_slots_satisfied = False
                    break
            if all_slots_satisfied:
                proposals.append(BookableSlotProposal(start_time=window_start, end_time=window_end))
            cursor = cursor + slot_step
        return proposals

    def _split_calendars_by_management(self, calendar_ids: set[int]) -> tuple[set[int], set[int]]:
        managed_ids: set[int] = set()
        unmanaged_ids: set[int] = set()
        for cid, managed in (
            Calendar.objects.filter_by_organization(self.organization.id)
            .filter(id__in=calendar_ids)
            .values_list("id", "manage_available_windows")
        ):
            if managed:
                managed_ids.add(cid)
            else:
                unmanaged_ids.add(cid)
        return managed_ids, unmanaged_ids

    def _fetch_available_spans(
        self,
        managed_ids: set[int],
        search_window_start: datetime.datetime,
        search_window_end: datetime.datetime,
    ) -> dict[int, list[tuple[datetime.datetime, datetime.datetime]]]:
        spans: dict[int, list[tuple[datetime.datetime, datetime.datetime]]] = {}
        if not managed_ids:
            return spans
        for row in (
            AvailableTime.objects.filter_by_organization(self.organization.id)
            .filter(
                calendar_fk_id__in=managed_ids,
                start_time__lte=search_window_end,
                end_time__gte=search_window_start,
            )
            .values("calendar_fk_id", "start_time", "end_time")
        ):
            spans.setdefault(row["calendar_fk_id"], []).append((row["start_time"], row["end_time"]))
        return spans

    def _fetch_blocking_spans(
        self,
        unmanaged_ids: set[int],
        search_window_start: datetime.datetime,
        search_window_end: datetime.datetime,
        *,
        with_bulk_modifications: bool,
    ) -> dict[int, list[tuple[datetime.datetime, datetime.datetime]]]:
        spans: dict[int, list[tuple[datetime.datetime, datetime.datetime]]] = {}
        if not unmanaged_ids:
            return spans

        if with_bulk_modifications:
            events_qs = CalendarEvent.objects.filter_by_organization(
                self.organization.id
            ).annotate_recurring_occurrences_with_bulk_modifications_on_date_range(
                search_window_start, search_window_end
            )
        else:
            events_qs = CalendarEvent.objects.filter_by_organization(
                self.organization.id
            ).annotate_recurring_occurrences_on_date_range(search_window_start, search_window_end)

        overlap_filter = (
            Q(start_time__range=(search_window_start, search_window_end))
            | Q(end_time__range=(search_window_start, search_window_end))
            | Q(start_time__lte=search_window_start, end_time__gte=search_window_end)
            | Q(recurring_occurrences__len__gt=0)
        )

        for ev in events_qs.filter(overlap_filter, calendar_fk_id__in=unmanaged_ids).values(
            "calendar_fk_id", "start_time", "end_time", "recurring_occurrences"
        ):
            bucket = spans.setdefault(ev["calendar_fk_id"], [])
            if ev["start_time"] and ev["end_time"]:
                bucket.append((ev["start_time"], ev["end_time"]))
            for occ in ev["recurring_occurrences"] or ():
                occ_start = datetime.datetime.fromisoformat(occ["start_time"])
                occ_end = datetime.datetime.fromisoformat(occ["end_time"])
                bucket.append((occ_start, occ_end))

        for bt in (
            BlockedTime.objects.filter_by_organization(self.organization.id)
            .filter(
                Q(start_time__range=(search_window_start, search_window_end))
                | Q(end_time__range=(search_window_start, search_window_end))
                | Q(
                    start_time__lte=search_window_start,
                    end_time__gte=search_window_end,
                ),
                calendar_fk_id__in=unmanaged_ids,
            )
            .values("calendar_fk_id", "start_time", "end_time")
        ):
            spans.setdefault(bt["calendar_fk_id"], []).append((bt["start_time"], bt["end_time"]))
        return spans
