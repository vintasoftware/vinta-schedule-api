import datetime
from collections.abc import Iterable
from typing import TYPE_CHECKING, Annotated, cast

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from dependency_injector.wiring import Provide, inject

from audit.constants import AuditAction
from audit.diff import compute_diff
from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.exceptions import (
    BookingPolicyViolationError,
    CalendarGroupHasFutureEventsError,
    CalendarGroupScopedRuleViolationError,
    CalendarGroupSlotConfigNotFoundError,
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
    CalendarOwnership,
    RecurrenceRule,
)
from calendar_integration.querysets import CalendarEventQuerySet
from calendar_integration.services import slot_engine
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.calendar_service_utils import (
    convert_naive_utc_datetime_to_timezone as _convert_naive_utc_datetime_to_timezone,
)
from calendar_integration.services.calendar_service_utils import (
    resolve_acting_single_use_token,
)
from calendar_integration.services.dataclasses import (
    BookableSlotProposal,
    CalendarEventInputData,
    CalendarGroupEventInputData,
    CalendarGroupInputData,
    CalendarGroupRangeAvailability,
    CalendarGroupSlotAvailability,
    CalendarGroupSlotInputData,
    EffectivePolicy,
    EventAttendanceInputData,
    EventExternalAttendanceInputData,
    ExternalAttendeeInputData,
    GroupScopedAvailabilityWriteResult,
    ResourceAllocationInputData,
)
from organizations.models import Organization
from payments.billing_constants import LimitedResource
from payments.exceptions import OverLimitError
from users.models import User


if TYPE_CHECKING:
    from audit.services import AuditService
    from calendar_integration.services.booking_policy_service import BookingPolicyService
    from calendar_integration.services.calendar_service import CalendarService
    from payments.services.entitlement_service import EntitlementService


def _time_range_fully_covered(
    windows: Iterable[tuple[datetime.datetime, datetime.datetime]],
    start: datetime.datetime,
    end: datetime.datetime,
) -> bool:
    """Whether ``[start, end)`` is fully covered by the union of ``windows``.

    Used to decide whether a confirmed future booking still falls inside a
    calendar's group-scoped availability configuration after a write.
    ``windows`` may be unsorted or overlapping; each is clipped against the
    shrinking set of remaining uncovered gaps.
    """
    remaining = [(start, end)]
    for window_start, window_end in windows:
        next_remaining: list[tuple[datetime.datetime, datetime.datetime]] = []
        for gap_start, gap_end in remaining:
            if window_end <= gap_start or window_start >= gap_end:
                # No overlap with this gap -- carry it forward untouched.
                next_remaining.append((gap_start, gap_end))
                continue
            if window_start > gap_start:
                next_remaining.append((gap_start, window_start))
            if window_end < gap_end:
                next_remaining.append((window_end, gap_end))
        remaining = next_remaining
        if not remaining:
            break
    return not remaining


class CalendarGroupService:
    organization: Organization | None

    @inject
    def __init__(
        self,
        calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
        calendar_permission_service: Annotated[
            "CalendarPermissionService | None", Provide["calendar_permission_service"]
        ] = None,
        audit_service: Annotated["AuditService | None", Provide["audit_service"]] = None,
        booking_policy_service: Annotated[
            "BookingPolicyService | None", Provide["booking_policy_service"]
        ] = None,
        entitlement_service: Annotated[
            "EntitlementService | None", Provide["entitlement_service"]
        ] = None,
    ) -> None:
        self.organization = None
        self.calendar_service = calendar_service
        self.calendar_permission_service = calendar_permission_service
        self.audit_service = audit_service
        self.booking_policy_service = booking_policy_service
        self.entitlement_service = entitlement_service

    def _audit_group_write(
        self,
        action: str,
        subject_instance: object,
        diff: dict | None = None,
    ) -> None:
        """Emit an audit record for a calendar-group business write.

        The acting principal is taken from the bound ``calendar_service``'s auth
        context (the facade carries ``user_or_token``); when unavailable the actor
        resolves to the system. No-op when no ``audit_service`` / ``organization`` is
        bound, so instrumentation never breaks a write path.
        """
        if self.audit_service is None or self.organization is None:
            return
        user_or_token = getattr(self.calendar_service, "user_or_token", None)
        permission_service = getattr(self.calendar_service, "calendar_permission_service", None)
        self.audit_service.record(
            organization_id=self.organization.id,
            action=action,
            actor=self.audit_service.actor_from_user_or_token(
                user_or_token,
                self.organization.id,
                single_use_token=resolve_acting_single_use_token(user_or_token, permission_service),
            ),
            subject=self.audit_service.subject_from_instance(subject_instance),
            diff=diff,
        )

    def initialize(self, organization: Organization) -> None:
        """Initialize the service with the tenant organization.

        For methods that need to delegate event creation to `CalendarService`
        (i.e. `create_grouped_event`), the caller must also separately initialize
        or authenticate `self.calendar_service` with the same organization — the
        grouped-event flow needs external-provider adapters if any of the
        selected calendars is backed by one.

        Also propagates the org context to ``booking_policy_service`` when present,
        so the policy resolver is bound to the same tenant.
        """
        self.organization = organization
        if self.booking_policy_service is not None:
            self.booking_policy_service.initialize(organization)

    def _assert_initialized(self) -> None:
        if self.organization is None:
            raise CalendarServiceOrganizationNotSetError(
                "CalendarGroupService requires an organization. Call initialize()."
            )

    def _check_not_restricted(self) -> None:
        """Raise ``OverLimitError`` if this service's organization's billing root is
        ``RESTRICTED`` -- the same check the sibling
        ``CalendarService._check_not_restricted`` makes before an update/delete write.

        Honors the same bypass source those siblings do: a no-op when no
        ``entitlement_service`` is injected, or when the bound ``calendar_service``
        is in bypass mode (``authenticate(bypass_limits=True)``). Every other blocked
        service short-circuits on that bypass flag; checking it here too keeps a
        legitimate bypass path (management commands, repair scripts) from being
        stopped at this one check while every peer check lets it through.
        """
        if self.entitlement_service is None:
            return
        if getattr(self.calendar_service, "_bypass_entitlement_limits", False):
            return
        self.entitlement_service.check_not_restricted(cast("Organization", self.organization))

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
        calendar_to_slot_name: dict[int, str] = {}
        for slot_data in slots:
            if slot_data.name in seen_slot_names:
                raise CalendarGroupValidationError(f"Duplicate slot name: {slot_data.name!r}.")
            seen_slot_names.add(slot_data.name)

            # A calendar may belong to at most one slot per group. Availability and
            # bookable-slot computation count each slot's pool independently, so an
            # overlapping calendar would be double-counted and the group reported
            # bookable when no valid disjoint assignment exists.
            for cid in slot_data.calendar_ids:
                other_slot = calendar_to_slot_name.get(cid)
                if other_slot is not None and other_slot != slot_data.name:
                    raise CalendarGroupValidationError(
                        f"Calendar {cid} appears in multiple slots "
                        f"({other_slot!r} and {slot_data.name!r}). A calendar may "
                        f"belong to at most one slot per group."
                    )
                calendar_to_slot_name[cid] = slot_data.name

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
    def create_group(
        self, data: CalendarGroupInputData, bypass_limits: bool = False
    ) -> CalendarGroup:
        """Create a CalendarGroup with its slots and memberships.

        :param bypass_limits: When True, skips the ``calendar_groups`` limit guard below.
            Only management commands and one-off repair scripts should pass this -- never
            a request-handling path.
        :raises OverLimitError: When the organization is at its effective ``calendar_groups``
            ceiling. Nothing is created. Checked and locked (``SELECT ... FOR UPDATE`` on the
            billing root's subscription) inside this method's own transaction, so two
            concurrent creates for the last unit of capacity serialize on that row.
        """
        self._assert_initialized()
        # _assert_initialized() raises above when None; cast narrows the type for
        # the entitlement check below (mypy does not infer this across the call).
        organization = cast("Organization", self.organization)
        slots_data, _ = self._validate_slots_input(data.slots)

        if not bypass_limits and self.entitlement_service is not None:
            result = self.entitlement_service.check_limit(
                organization, LimitedResource.CALENDAR_GROUPS, lock=True
            )
            if not result.allowed:
                raise OverLimitError.from_check_result(result)

        # When accepts_public_scheduling is provided, use it; otherwise default to False (private).
        accepts_public_scheduling = (
            data.accepts_public_scheduling if data.accepts_public_scheduling is not None else False
        )

        group = CalendarGroup.objects.create(
            organization=self.organization,
            name=data.name,
            description=data.description,
            accepts_public_scheduling=accepts_public_scheduling,
        )
        self._create_slots(group, slots_data)
        self._audit_group_write(AuditAction.CREATE, group)
        return group

    @transaction.atomic()
    def update_group(self, group_id: int, data: CalendarGroupInputData) -> CalendarGroup:
        """Reconcile a CalendarGroup's slots and memberships with `data`.

        Slots are matched by name. Removing a slot, or removing a calendar from an
        existing slot's pool, is refused if any future-booked event references it.
        """
        self._assert_initialized()
        self._check_not_restricted()
        group = self._get_group_by_id(group_id)
        slots_data, _ = self._validate_slots_input(data.slots)

        before = {
            "name": group.name,
            "description": group.description,
            "accepts_public_scheduling": group.accepts_public_scheduling,
        }
        group.name = data.name
        group.description = data.description
        # Only update accepts_public_scheduling if it is provided (not None).
        if data.accepts_public_scheduling is not None:
            group.accepts_public_scheduling = data.accepts_public_scheduling

        # Build update_fields dynamically to avoid writing privacy when not provided.
        update_fields = ["name", "description", "modified"]
        if data.accepts_public_scheduling is not None:
            update_fields.append("accepts_public_scheduling")

        group.save(update_fields=update_fields)

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

        after = {
            "name": group.name,
            "description": group.description,
            "accepts_public_scheduling": group.accepts_public_scheduling,
        }
        self._audit_group_write(AuditAction.UPDATE, group, diff=compute_diff(before, after))

        return group

    @transaction.atomic()
    def delete_group(self, group_id: int) -> None:
        """Delete a CalendarGroup. Refuses if any events (past or future) reference
        it, matching the PROTECT FK on `CalendarEvent.calendar_group`."""
        self._assert_initialized()
        self._check_not_restricted()
        group = self._get_group_by_id(group_id)

        if (
            CalendarEvent.objects.filter_by_organization(self.organization.id)
            .filter(calendar_group_fk=group)
            .exists()
        ):
            raise CalendarGroupHasFutureEventsError(
                "Cannot delete CalendarGroup because it has bookings."
            )

        # Build the audit subject before the row is deleted (pk is needed).
        self._audit_group_write(AuditAction.DELETE, group)
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

            # Delete group-scoped windows for the removed calendars.
            # The FK on AvailableTime.group_slot → CalendarGroupSlot cascades on
            # SLOT deletion only, not on membership removal, so we must explicitly
            # clean up the orphaned rows here. Each window deletion is audited
            # individually because the group-update diff only captures name/description
            # /accepts_public_scheduling, not membership or window changes.
            # TODO(Phase 2a, 3a): BlockedTime.for_group_slot() and quota rules
            # must extend this cleanup when those phases add their group-scoped rows,
            # using the same pattern: delete rows for removed calendars in to_remove.
            org_id = cast(Organization, self.organization).id
            windows_to_delete = list(
                AvailableTime.objects.unscoped()
                .filter_by_organization(org_id)
                .filter(group_slot_fk=slot, calendar_fk_id__in=to_remove)
            )

            # Audit each window deletion before removing it.
            for window in windows_to_delete:
                if self.audit_service is None or self.organization is None:
                    break
                user_or_token = getattr(self.calendar_service, "user_or_token", None)
                permission_service = getattr(
                    self.calendar_service, "calendar_permission_service", None
                )
                self.audit_service.record(
                    organization_id=self.organization.id,
                    action=AuditAction.DELETE,
                    actor=self.audit_service.actor_from_user_or_token(
                        user_or_token,
                        self.organization.id,
                        single_use_token=resolve_acting_single_use_token(
                            user_or_token, permission_service
                        ),
                    ),
                    subject=self.audit_service.subject_from_instance(window),
                )

            # Delete the windows after auditing them.
            AvailableTime.objects.unscoped().filter_by_organization(org_id).filter(
                group_slot_fk=slot, calendar_fk_id__in=to_remove
            ).delete()

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
    # Group-scoped availability windows (Phase 1a of
    # CALENDAR_GROUP_SCOPED_AVAILABILITY -- writes)
    # ------------------------------------------------------------------

    def _resolve_group_scoped_membership(
        self, group_slot_id: int, calendar_id: int
    ) -> CalendarGroupSlotMembership:
        """Resolve the roster entry (calendar, group slot) a group-scoped
        availability write targets.

        Raises the same not-found-shaped ``CalendarGroupSlotConfigNotFoundError``
        whether the slot doesn't exist, the calendar doesn't exist, or the
        calendar simply isn't a member of that slot -- callers must not be able
        to tell which case applies from the error alone.
        """
        org_id = cast(Organization, self.organization).id
        try:
            return (
                CalendarGroupSlotMembership.objects.filter_by_organization(org_id)
                .select_related("slot", "calendar")
                .get(slot_fk_id=group_slot_id, calendar_fk_id=calendar_id)
            )
        except CalendarGroupSlotMembership.DoesNotExist:
            raise CalendarGroupSlotConfigNotFoundError() from None

    def _get_group_scoped_window(self, window_id: int) -> AvailableTime:
        """Fetch a group-scoped ``AvailableTime`` row by id, scoped to this org.

        Reads through the ``unscoped`` accessor (never the default manager,
        which excludes group-scoped rows) and requires ``group_slot`` to be
        set, so an id belonging to a base row raises the same not-found error
        as a genuinely missing id.
        """
        org_id = cast(Organization, self.organization).id
        try:
            return (
                AvailableTime.objects.unscoped()
                .filter_by_organization(org_id)
                .select_related("group_slot", "calendar", "recurrence_rule")
                .get(id=window_id, group_slot_fk__isnull=False)
            )
        except AvailableTime.DoesNotExist:
            raise CalendarGroupSlotConfigNotFoundError() from None

    def _authorize_group_scoped_write(
        self, acting_user: User, calendar: Calendar, group_slot: CalendarGroupSlot
    ) -> None:
        """Gate a group-scoped availability write to the calendar's owner (within
        a group they can see) or an org admin.

        Fails closed -- raises when no ``calendar_permission_service`` is bound,
        rather than silently allowing the write, since this path exists
        specifically to be permission-gated (spec goal 4). Denial and
        "the roster entry doesn't exist" share the exact same exception, so a
        member cannot learn a group exists through the error shape.
        """
        if self.calendar_permission_service is None or not (
            self.calendar_permission_service.can_manage_group_scoped_calendar_config(
                user=acting_user, calendar=calendar, group_slot=group_slot
            )
        ):
            raise CalendarGroupSlotConfigNotFoundError()

    def _create_recurrence_rule_if_needed(self, rrule_string: str | None) -> RecurrenceRule | None:
        """Create (and persist) a ``RecurrenceRule`` from an RRULE string, or None.

        Self-contained variant of the identically-named helper
        ``CalendarService``/``AvailabilityService`` expose through their host
        protocol -- this write path does not require a bound
        ``calendar_service``, so it builds the ``RecurrenceRule`` directly.
        """
        if not rrule_string:
            return None
        organization = cast(Organization, self.organization)
        recurrence_rule = RecurrenceRule.from_rrule_string(rrule_string, organization)
        recurrence_rule.save()
        return recurrence_rule

    def _audit_group_scoped_availability_write(
        self,
        action: str,
        acting_user: User,
        subject_instance: AvailableTime,
        diff: dict | None = None,
    ) -> None:
        """Emit an audit record for a group-scoped availability window write.

        Unlike ``_audit_group_write`` (which resolves the actor from a bound
        ``calendar_service``'s auth context), these write methods take the
        acting principal explicitly -- they are reachable without a bound
        ``calendar_service``. No-op when no ``audit_service`` / ``organization``
        is bound, so instrumentation never breaks a write path.
        """
        if self.audit_service is None or self.organization is None:
            return
        self.audit_service.record(
            organization_id=self.organization.id,
            action=action,
            actor=self.audit_service.actor_from_user_or_token(acting_user, self.organization.id),
            subject=self.audit_service.subject_from_instance(subject_instance),
            diff=diff,
        )

    def _group_scoped_available_times_expanded(
        self,
        calendar_id: int,
        group_slot_id: int,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[AvailableTime]:
        """Expand every group-scoped ``AvailableTime`` for ``(calendar, group_slot)``
        that overlaps ``[start_date, end_date)``, recurrence included.

        Mirrors ``AvailabilityService.get_available_times_expanded``, but reads
        through the group-scoped accessor instead of the default (base-rows-only)
        manager. Delegates to ``slot_engine.expand_group_scoped_available_times``
        (CALENDAR_GROUP_SCOPED_AVAILABILITY Phase 1b), the single-pair case of the
        same batched implementation the discovery-side fetch uses, so both paths
        share the annotate-first strategy that keeps
        ``get_occurrences_in_range()`` from falling through to
        ``RecurringMixin``'s internal exception-instance re-fetch -- which goes
        through the *default*, base-rows-only manager and would otherwise
        silently return nothing for a group-scoped master's exceptions (see the
        Phase 0 carry-forward note).
        """
        org_id = cast(Organization, self.organization).id
        return slot_engine.expand_group_scoped_available_times(
            org_id, [group_slot_id], [calendar_id], start_date, end_date
        )

    def _find_orphaned_bookings(
        self, calendar_id: int, group_slot: CalendarGroupSlot, now: datetime.datetime
    ) -> list[CalendarEvent]:
        """Confirmed future bookings in ``group_slot`` for ``calendar_id`` that
        fall outside the calendar's current group-scoped availability
        configuration.

        Runs against every group-scoped window that currently exists for this
        ``(calendar, slot)`` pair -- the whole union, not just the one row a
        caller just wrote -- so a booking is reported exactly when the
        calendar's configured availability, as it stands after the write, no
        longer covers it. Nothing here is cancelled or modified; this is
        purely a read (spec UC-6).

        Performance: expands all group-scoped windows ONCE over the union range
        covering all candidate bookings (min start → max end), then checks each
        booking against that single cached expansion, rather than expanding
        once per booking (O(N) → O(1) expansions).
        """
        org_id = cast(Organization, self.organization).id
        selections = (
            CalendarEventGroupSelection.objects.filter_by_organization(org_id)
            .filter(
                slot_fk=group_slot,
                calendar_fk_id=calendar_id,
                event_fk__start_time__gt=now,
            )
            .select_related("event")
        )

        selections_list = list(selections)
        if not selections_list:
            return []

        # Find the union range covering all candidate bookings: min start → max end.
        min_start = min(sel.event.start_time for sel in selections_list)
        max_end = max(sel.event.end_time for sel in selections_list)

        # Expand ALL group-scoped windows once over the union range.
        all_windows = self._group_scoped_available_times_expanded(
            calendar_id, group_slot.id, min_start, max_end
        )

        # Check each booking against the single cached expansion.
        orphaned: list[CalendarEvent] = []
        for selection in selections_list:
            event = selection.event
            covered = _time_range_fully_covered(
                ((w.start_time, w.end_time) for w in all_windows),
                event.start_time,
                event.end_time,
            )
            if not covered:
                orphaned.append(event)
        return orphaned

    @transaction.atomic()
    def create_group_scoped_availability_window(
        self,
        acting_user: User,
        group_slot_id: int,
        calendar_id: int,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        tz: str,
        rrule_string: str | None = None,
        now: datetime.datetime | None = None,
    ) -> GroupScopedAvailabilityWriteResult:
        """Create a group-scoped availability window for ``(calendar, group_slot)``.

        Writes through the explicit group-scoped accessor
        (``AvailableTime.objects.unscoped().create(..., group_slot=...)``),
        never the default manager. Carries the same recurrence expressiveness
        as a base ``AvailableTime`` -- pass ``rrule_string`` for a recurring
        window. Permission-gated: ``acting_user`` must own the calendar or be
        an org admin (see
        ``CalendarPermissionService.can_manage_group_scoped_calendar_config``).

        On creating the FIRST group-scoped window for a (calendar, group_slot),
        collects confirmed future bookings that fall outside the new window and
        returns them as orphaned_bookings (spec UC-6). Creating subsequent
        windows only widens the union (can never orphan), so orphaned-booking
        detection runs only on the first create. Nothing is cancelled.
        """
        self._assert_initialized()
        if now is None:
            now = timezone.now()

        organization = cast(Organization, self.organization)
        membership = self._resolve_group_scoped_membership(group_slot_id, calendar_id)
        self._authorize_group_scoped_write(acting_user, membership.calendar, membership.slot)

        # Check if this will be the calendar's FIRST window in this slot.
        # If so, narrowing is happening and we must detect orphaned bookings.
        existing_window_count = (
            AvailableTime.objects.for_group_slot(group_slot_id)
            .filter_by_organization(organization.id)
            .filter(calendar_fk_id=calendar_id)
            .count()
        )
        is_first_window = existing_window_count == 0

        recurrence_rule = self._create_recurrence_rule_if_needed(rrule_string)
        window = AvailableTime.objects.unscoped().create(
            organization=organization,
            calendar=membership.calendar,
            group_slot=membership.slot,
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
            timezone=tz,
            recurrence_rule=recurrence_rule,
        )
        self._audit_group_scoped_availability_write(AuditAction.CREATE, acting_user, window)

        # Detect orphaned bookings only on the first window create.
        orphaned_bookings: list[CalendarEvent] = []
        if is_first_window:
            orphaned_bookings = self._find_orphaned_bookings(
                calendar_id=calendar_id,
                group_slot=membership.slot,
                now=now,
            )

        return GroupScopedAvailabilityWriteResult(
            window=window, orphaned_bookings=orphaned_bookings
        )

    @transaction.atomic()
    def update_group_scoped_availability_window(
        self,
        acting_user: User,
        window_id: int,
        start_time: datetime.datetime | None = None,
        end_time: datetime.datetime | None = None,
        tz: str | None = None,
        rrule_string: str | None = None,
        now: datetime.datetime | None = None,
    ) -> GroupScopedAvailabilityWriteResult:
        """Partially update a group-scoped availability window (only provided
        fields change -- mirrors ``AvailabilityService.update_blocked_time``).

        After the update is applied, every confirmed future booking in the
        window's group slot for its calendar that no longer falls inside the
        calendar's group-scoped configuration is collected and returned.
        Narrowing a window never cancels or edits a booking (spec UC-6) --
        the caller decides what to do with each one.
        """
        self._assert_initialized()
        if now is None:
            now = timezone.now()

        window = self._get_group_scoped_window(window_id)
        self._authorize_group_scoped_write(acting_user, window.calendar, window.group_slot)

        before = {
            "start_time_tz_unaware": window.start_time_tz_unaware.isoformat(),
            "end_time_tz_unaware": window.end_time_tz_unaware.isoformat(),
            "timezone": window.timezone,
            "rrule": window.recurrence_rule.to_rrule_string() if window.recurrence_rule else None,
        }

        update_fields: list[str] = []
        if start_time is not None:
            window.start_time_tz_unaware = start_time
            update_fields.append("start_time_tz_unaware")
        if end_time is not None:
            window.end_time_tz_unaware = end_time
            update_fields.append("end_time_tz_unaware")
        if tz is not None:
            window.timezone = tz
            update_fields.append("timezone")
        if rrule_string is not None:
            window.recurrence_rule = self._create_recurrence_rule_if_needed(rrule_string)
            # Assigning through the ForeignObject property name ("recurrence_rule")
            # sets the underlying concrete column ("recurrence_rule_fk"); `save`'s
            # `update_fields` must name the concrete field.
            update_fields.append("recurrence_rule_fk")

        if update_fields:
            window.save(update_fields=[*update_fields, "modified"])

        after = {
            "start_time_tz_unaware": window.start_time_tz_unaware.isoformat(),
            "end_time_tz_unaware": window.end_time_tz_unaware.isoformat(),
            "timezone": window.timezone,
            "rrule": window.recurrence_rule.to_rrule_string() if window.recurrence_rule else None,
        }
        self._audit_group_scoped_availability_write(
            AuditAction.UPDATE, acting_user, window, diff=compute_diff(before, after)
        )

        orphaned_bookings = self._find_orphaned_bookings(
            calendar_id=window.calendar_fk_id,  # type: ignore[arg-type]
            group_slot=window.group_slot,
            now=now,
        )
        return GroupScopedAvailabilityWriteResult(
            window=window, orphaned_bookings=orphaned_bookings
        )

    @transaction.atomic()
    def delete_group_scoped_availability_window(self, acting_user: User, window_id: int) -> None:
        """Delete a group-scoped availability window (a single ``AvailableTime`` row).

        A recurring window is stored as one row; deleting it removes the whole
        series (mirrors ``AvailabilityService.delete_blocked_time``). No
        orphaned-booking report is computed here -- the spec scopes that to
        narrowing UPDATEs (UC-6). Removing a calendar from a slot's roster
        entirely (which cascades away its windows per the Phase 0 schema
        constraint) is a distinct action, exercised through ``update_group``.
        """
        self._assert_initialized()
        window = self._get_group_scoped_window(window_id)
        self._authorize_group_scoped_write(acting_user, window.calendar, window.group_slot)

        self._audit_group_scoped_availability_write(AuditAction.DELETE, acting_user, window)
        window.delete()

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

    def _slot_pools_with_group_scoped_flags(
        self, slots: Iterable[CalendarGroupSlot]
    ) -> tuple[dict[int, set[int]], dict[int, set[int]]]:
        """Build the (slot_id -> calendar_id pool) map, folding in a per-row
        ``EXISTS`` subquery flagging which pool calendars have ANY group-scoped
        availability window configured for that slot (regardless of whether it
        overlaps a caller's search window).

        This is the Phase 1b self-gating early-out mechanism
        (``CALENDAR_GROUP_SCOPED_AVAILABILITY``): the ``EXISTS`` clause is
        folded into the SAME per-slot membership query every caller of this
        method already issued before this phase, so an unconfigured group
        costs exactly as many round trips as it did before -- zero added
        queries. Only when the returned "configured" map is non-empty for a
        slot does the caller go on to fetch the actual group-scoped spans (a
        fixed, non-per-candidate number of additional queries -- see
        ``slot_engine.fetch_group_scoped_available_spans``).

        Returns ``(slot_pool_by_id, group_scoped_calendar_ids_by_slot)``. The
        second mapping omits a slot entirely when nothing in its pool is
        configured, so ``bool(group_scoped_calendar_ids_by_slot)`` alone tells
        a caller whether ANY group-scoped window exists anywhere in the group.
        """
        org_id = cast(Organization, self.organization).id
        slot_pool_by_id: dict[int, set[int]] = {}
        group_scoped_calendar_ids_by_slot: dict[int, set[int]] = {}
        for s in slots:
            rows = (
                CalendarGroupSlotMembership.objects.filter_by_organization(org_id)
                .filter(slot_fk=s)
                .annotate(
                    has_group_scoped_window=Exists(
                        AvailableTime.objects.unscoped()
                        .filter_by_organization(org_id)
                        .filter(calendar_fk_id=OuterRef("calendar_fk_id"), group_slot_fk_id=s.id)
                    )
                )
                .values_list("calendar_fk_id", "has_group_scoped_window")
            )
            pool: set[int] = set()
            configured: set[int] = set()
            for cid, has_window in rows:
                pool.add(cid)
                if has_window:
                    configured.add(cid)
            slot_pool_by_id[s.id] = pool
            if configured:
                group_scoped_calendar_ids_by_slot[s.id] = configured
        return slot_pool_by_id, group_scoped_calendar_ids_by_slot

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

        Group-scoped availability windows (``CALENDAR_GROUP_SCOPED_AVAILABILITY``
        Phase 1b) are intersected in AFTER base availability: a calendar with no
        group-scoped window configured for a slot is unaffected (fall-through
        default, zero added queries -- see
        ``_slot_pools_with_group_scoped_flags``); a calendar WITH one is listed
        as available for a range only when that range is fully covered by at
        least one of its group-scoped windows -- narrowing only, never widening.
        """
        self._assert_initialized()
        group = self._get_group_by_id(group_id)
        ranges = list(ranges)

        slots = list(group.slots.all())
        slot_pool_by_id, group_scoped_calendar_ids_by_slot = (
            self._slot_pools_with_group_scoped_flags(slots)
        )

        # Self-gating early-out: only fetch expanded group-scoped spans when at
        # least one calendar anywhere in the group actually has one configured.
        group_scoped_spans_by_slot: slot_engine.GroupScopedSpansBySlot = {}
        if group_scoped_calendar_ids_by_slot and ranges:
            configured_slot_ids = list(group_scoped_calendar_ids_by_slot.keys())
            configured_calendar_ids: set[int] = set()
            for ids in group_scoped_calendar_ids_by_slot.values():
                configured_calendar_ids.update(ids)
            union_start = min(start for start, _ in ranges)
            union_end = max(end for _, end in ranges)
            group_scoped_spans_by_slot = slot_engine.fetch_group_scoped_available_spans(
                self.organization.id,
                configured_slot_ids,
                configured_calendar_ids,
                union_start,
                union_end,
            )

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
            slot_results = []
            for s in slots:
                base_available = slot_pool_by_id[s.id] & available_ids
                configured_ids = group_scoped_calendar_ids_by_slot.get(s.id)
                if not configured_ids:
                    final_available = base_available
                else:
                    spans_for_slot = group_scoped_spans_by_slot.get(s.id, {})
                    final_available = {
                        cid
                        for cid in base_available
                        if cid not in configured_ids
                        or slot_engine.window_fully_covered_by_spans(
                            spans_for_slot.get(cid, ()), start, end
                        )
                    }
                slot_results.append(
                    CalendarGroupSlotAvailability(
                        slot_id=s.id,
                        available_calendar_ids=sorted(final_available),
                        required_count=s.required_count,
                    )
                )
            results.append(
                CalendarGroupRangeAvailability(
                    start_time=start,
                    end_time=end,
                    slots=slot_results,
                )
            )
        return results

    # ------------------------------------------------------------------
    # Booking policy enforcement (group path)
    # ------------------------------------------------------------------

    def _check_group_booking_policy(
        self,
        group: CalendarGroup,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        now: datetime.datetime,
    ) -> None:
        """Enforce the resolved EffectivePolicy for a group booking request.

        Mirrors the shape of ``CalendarService._check_booking_policy`` for
        the group write path.  Steps:

        1. Skip when ``booking_policy_service`` is not injected or when the resolved
           policy is ``EffectivePolicy.unconstrained()`` — preserving byte-for-byte
           pre-feature behavior (the data-presence check).
        2. Resolve the ``EffectivePolicy`` via ``resolve_for_group`` — the same
           resolver ``find_bookable_slots`` uses, so enforcement and
           discovery agree.
        3. Collect ALL participant calendar IDs across every slot pool of the group
           (the same ``all_calendar_ids`` set ``find_bookable_slots`` uses), so the
           buffer dead-zone check is conservative: any participant that would
           individually reject the window blocks the booking.
        4. Fetch buffer blocking spans across the full participant set (managed +
           unmanaged), widening the window by the buffer magnitudes — mirrors the
           buffer fetch in ``find_bookable_slots``.
        5. Build a single ``BookableSlotProposal`` and call
           ``slot_engine.apply_policy_filter``; empty result → raise
           ``BookingPolicyViolationError``.

        This check runs inside ``create_grouped_event``'s ``@transaction.atomic()``
        decorator, so a violation rolls back the entire group write — no events or
        blocked times are persisted.
        """
        if self.booking_policy_service is None or self.organization is None:
            # Data-presence gate: no service → skip (pre-feature behavior).
            return

        policy = self.booking_policy_service.resolve_for_group(group)
        if policy == EffectivePolicy.unconstrained():
            # No policy anywhere for this group → skip all enforcement.
            return

        # Collect ALL calendar IDs in the group's slot pools — the same set
        # find_bookable_slots uses so enforcement matches discovery exactly.
        org_id = cast(Organization, self.organization).id
        slots = list(group.slots.all())
        all_calendar_ids: set[int] = set()
        for slot in slots:
            cal_ids = (
                CalendarGroupSlotMembership.objects.filter_by_organization(org_id)
                .filter(slot_fk=slot)
                .values_list("calendar_fk_id", flat=True)
            )
            all_calendar_ids.update(cal_ids)

        no_buffer = policy.buffer_before <= datetime.timedelta(
            0
        ) and policy.buffer_after <= datetime.timedelta(0)
        if no_buffer:
            buffer_blocking_spans: slot_engine.SpansByCalendarId = {}
        else:
            buffer_blocking_spans = slot_engine.fetch_blocking_spans(
                org_id,
                all_calendar_ids,
                start_time - policy.buffer_after,
                end_time + policy.buffer_before,
                with_bulk_modifications=False,
            )

        proposal = BookableSlotProposal(start_time=start_time, end_time=end_time)
        allowed = slot_engine.apply_policy_filter([proposal], policy, now, buffer_blocking_spans)
        if not allowed:
            raise BookingPolicyViolationError()

    # ------------------------------------------------------------------
    # Grouped event creation
    # ------------------------------------------------------------------
    @transaction.atomic()
    def create_grouped_event(self, data: CalendarGroupEventInputData) -> CalendarEvent:
        """Create an event booked through a CalendarGroup.

        Persistence strategy: the event is created
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

        # --- Group-level authorization gate ---
        # The gate applies ONLY to codeless / unauthenticated booking paths — i.e.
        # when the caller is not an authenticated ``User``.  Authenticated users
        # (group owners, org admins) bypass this check: they already passed Django's
        # view-level authentication and the CalendarGroupPermission object check.
        #
        # When the calendar service is initialized with a ``User``, the booking is
        # an authenticated internal path and the gate is skipped.  For all other
        # paths (no user, a raw token string, a SystemUser public-API token, or
        # None), ``can_perform_group_scheduling`` decides:
        #   1. ``group.accepts_public_scheduling=True`` → allow (codeless public).
        #   2. A group-scoped token/code with CREATE permission → allow.
        #   3. Otherwise → PermissionDenied.
        calendar_service_user = (
            getattr(self.calendar_service, "user_or_token", None)
            if self.calendar_service is not None
            else None
        )
        caller_is_authenticated_user = isinstance(calendar_service_user, User)

        if not caller_is_authenticated_user:
            if (
                self.calendar_permission_service is None
                or not self.calendar_permission_service.can_perform_group_scheduling(
                    group=group,
                )
            ):
                raise PermissionDenied(
                    "This group does not accept public scheduling. "
                    "A token or scheduling code is required."
                )

        slots = list(group.slots.order_by("order", "id"))
        if not slots:
            raise CalendarGroupValidationError("CalendarGroup has no slots to satisfy.")

        selections_by_slot_id = self._validate_selections(group, slots, data.slot_selections)
        all_selected_ids = {cid for sel in data.slot_selections for cid in sel.calendar_ids}
        self._assert_calendars_available(all_selected_ids, data.start_time, data.end_time)
        # Group-scoped availability windows (CALENDAR_GROUP_SCOPED_AVAILABILITY
        # Phase 1b): reject a directly-named calendar outside its configured
        # window, AFTER base availability -- narrowing only ever narrows.
        self._assert_calendars_within_group_scoped_windows(
            ((sel.slot_id, cid) for sel in data.slot_selections for cid in sel.calendar_ids),
            data.start_time,
            data.end_time,
        )

        # --- Booking policy enforcement ---
        # Runs inside the @transaction.atomic() so any violation rolls back the
        # entire write — no events or blocked times are persisted on rejection.
        self._check_group_booking_policy(
            group=group,
            start_time=data.start_time,
            end_time=data.end_time,
            now=timezone.now(),
        )

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

        # Signal to ``CalendarEventService.create_event`` that group-level
        # authorization has already been granted here. This prevents each member
        # calendar's own ``accepts_public_scheduling`` flag from independently
        # blocking a booking the group itself permits, without changing the
        # per-calendar gate for direct single-calendar bookings.
        event_input = CalendarEventInputData(
            title=data.title,
            description=data.description,
            start_time=data.start_time,
            end_time=data.end_time,
            timezone=data.timezone,
            attendances=merged_attendances,
            external_attendances=list(data.external_attendances),
            group_authorized=True,
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

    def cancel_grouped_event(self, event_id: int, delete_series: bool = False) -> None:
        """Cancel a grouped event by deleting the primary event and its linked non-primary BlockedTimes.

        The primary event is deleted via ``CalendarService.delete_event`` which also cascades
        the ``CalendarEventGroupSelection`` rows (FK on_delete=CASCADE).  Non-primary
        ``BlockedTime`` rows are linked only by the string ``external_id`` convention
        (not a FK), so they must be explicitly deleted here BEFORE the primary event is
        removed (so that the event_id is still meaningful for logging/debugging, though
        ordering within the caller's transaction does not affect correctness).

        Preconditions:
          - ``self.calendar_service`` is set and initialized/authenticated for the same
            organization (mirrors the requirement on ``create_grouped_event``).
          - The event identified by ``event_id`` must be a grouped event
            (``calendar_group_fk`` set) belonging to this service's organization.
        """
        self._assert_initialized()
        # Checked explicitly, and before any write -- ``delete_event``
        # below (called after this method has already deleted the non-primary
        # BlockedTime rows) would catch a RESTRICTED org too, but only after this
        # method's own deletes already ran outside of any transaction this method
        # itself opens. Failing here first avoids that partial-delete window
        # entirely rather than relying on a caller's surrounding transaction.
        self._check_not_restricted()
        if self.calendar_service is None:
            raise CalendarGroupValidationError(
                "CalendarGroupService.calendar_service must be provided to cancel grouped events."
            )
        if self.calendar_service.organization is None:
            raise CalendarGroupValidationError(
                "The injected CalendarService is not initialized with an organization."
            )
        if self.calendar_service.organization.id != self.organization.id:
            raise CalendarGroupValidationError(
                "The injected CalendarService is initialized with a different organization."
            )

        # Load the grouped event to validate it is truly grouped.
        try:
            event = CalendarEvent.objects.filter_by_organization(self.organization.id).get(
                id=event_id
            )
        except CalendarEvent.DoesNotExist:
            raise CalendarGroupValidationError(
                f"Event {event_id} not found in this organization."
            ) from None

        if event.calendar_group_fk_id is None:
            raise CalendarGroupValidationError(
                f"Event {event_id} is not a grouped event (calendar_group_fk is not set)."
            )

        primary_calendar_id: int = event.calendar_fk_id  # type: ignore[assignment]

        # Delete non-primary BlockedTimes first (string-linked, NOT cascaded).
        BlockedTime.objects.filter_by_organization(self.organization.id).filter(
            external_id__startswith=f"group-event-{event_id}-cal-"
        ).delete()

        # Delete the primary event (cascades CalendarEventGroupSelection via FK).
        self.calendar_service.delete_event(
            calendar_id=primary_calendar_id,
            event_id=event_id,
            delete_series=delete_series,
        )

    def reschedule_grouped_event(
        self,
        event_id: int,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        tz: str,
    ) -> CalendarEvent:
        """Reschedule a grouped event's times while preserving all other details.

        Changes only the start/end/timezone of the primary event (via
        ``CalendarService.update_event``, preserving title/description/attendances
        /external_attendances/resource_allocations so that only {RESCHEDULE}
        permission is required) and updates the linked non-primary BlockedTimes
        that were created by ``_create_non_primary_blocked_times``.

        Preconditions:
          - ``self.calendar_service`` is set and initialized/authenticated for the
            same organization (mirrors the requirement on ``create_grouped_event``).
          - The event identified by ``event_id`` must be a grouped event
            (``calendar_group_fk`` set) belonging to this service's organization.

        The event id is preserved — this is intentional: external systems (e.g.
        the Building Blocks integration) store it and rely on it remaining stable
        across reschedules.

        v1 limitation: non-primary calendar BASE availability is NOT re-checked
        on reschedule. Only the primary calendar's base availability is gated
        (in the mutation's availability check). Non-primary double-booking
        against base availability is therefore possible and intentionally
        unenforced for this time-only reschedule path.

        Group-scoped availability windows (``CALENDAR_GROUP_SCOPED_AVAILABILITY``
        Phase 1b) ARE re-checked here for every calendar currently selected for
        this event (primary and non-primary alike, via
        ``CalendarEventGroupSelection``) -- every enforcement surface must
        agree, so a narrowed calendar cannot dodge the window it would have
        been rejected for at booking time simply by rescheduling instead.
        """
        self._assert_initialized()
        # Checked explicitly and first, for the same reason as
        # ``cancel_grouped_event`` above -- this method writes non-primary
        # BlockedTime rows itself, ahead of (and outside of) the blocked
        # ``calendar_service.update_event`` call below.
        self._check_not_restricted()
        if self.calendar_service is None:
            raise CalendarGroupValidationError(
                "CalendarGroupService.calendar_service must be provided to reschedule grouped events."
            )
        if self.calendar_service.organization is None:
            raise CalendarGroupValidationError(
                "The injected CalendarService is not initialized with an organization."
            )
        if self.calendar_service.organization.id != self.organization.id:
            raise CalendarGroupValidationError(
                "The injected CalendarService is initialized with a different organization."
            )

        # Load the grouped event.
        try:
            event = (
                CalendarEvent.objects.filter_by_organization(self.organization.id)
                .select_related("calendar")
                .prefetch_related(
                    "attendances",
                    "resource_allocations",
                )
                .get(id=event_id)
            )
        except CalendarEvent.DoesNotExist:
            raise CalendarGroupValidationError(
                f"Event {event_id} not found in this organization."
            ) from None

        if event.calendar_group_fk_id is None:
            raise CalendarGroupValidationError(
                f"Event {event_id} is not a grouped event (calendar_group_fk is not set)."
            )

        # Group-scoped availability windows (CALENDAR_GROUP_SCOPED_AVAILABILITY
        # Phase 1b): reject the reschedule if ANY calendar currently selected
        # for this event is outside its group-scoped window for the NEW time.
        selection_pairs = list(
            CalendarEventGroupSelection.objects.filter_by_organization(
                cast(Organization, self.organization).id
            )
            .filter(event_fk=event)
            .values_list("slot_fk_id", "calendar_fk_id")
        )
        self._assert_calendars_within_group_scoped_windows(selection_pairs, start_time, end_time)

        # Build the update input preserving all non-time details so that only
        # RESCHEDULE permission is required (same approach as the single-calendar path).
        preserved_attendances = [
            EventAttendanceInputData(user_id=attendance.membership_user_id)
            for attendance in event.attendances.all()
            if attendance.membership_user_id is not None
        ]

        preserved_external_attendances = [
            EventExternalAttendanceInputData(
                external_attendee=ExternalAttendeeInputData(
                    email=ea.external_attendee_fk.email,  # type: ignore[union-attr]
                    name=ea.external_attendee_fk.name or "",  # type: ignore[union-attr]
                    id=ea.external_attendee_fk_id,  # type: ignore[union-attr]
                )
            )
            for ea in event.external_attendances.select_related("external_attendee")
        ]

        preserved_resource_allocations = [
            ResourceAllocationInputData(resource_id=ra.calendar_fk_id)  # type: ignore[arg-type]
            for ra in event.resource_allocations.all()
            if ra.calendar_fk_id
        ]

        event_data = CalendarEventInputData(
            title=event.title,
            description=event.description or "",
            start_time=start_time,
            end_time=end_time,
            timezone=tz,
            attendances=preserved_attendances,
            external_attendances=preserved_external_attendances,
            resource_allocations=preserved_resource_allocations,
        )

        primary_calendar_id: int = event.calendar_fk_id  # type: ignore[assignment]
        updated_event = self.calendar_service.update_event(
            primary_calendar_id, event_id, event_data
        )

        # Update the non-primary BlockedTimes linked to this grouped event.
        # They are identified by the external_id convention set in
        # _create_non_primary_blocked_times: ``group-event-{event.id}-cal-{cid}``.
        new_start_tz_unaware = _convert_naive_utc_datetime_to_timezone(start_time, tz)
        new_end_tz_unaware = _convert_naive_utc_datetime_to_timezone(end_time, tz)

        blocked_times_qs = BlockedTime.objects.filter_by_organization(self.organization.id).filter(
            external_id__startswith=f"group-event-{event_id}-cal-"
        )

        blocked_times = list(blocked_times_qs)
        for bt in blocked_times:
            bt.start_time_tz_unaware = new_start_tz_unaware
            bt.end_time_tz_unaware = new_end_tz_unaware
            bt.timezone = tz

        if blocked_times:
            BlockedTime.objects.bulk_update(
                blocked_times, ["start_time_tz_unaware", "end_time_tz_unaware", "timezone"]
            )

        return updated_event

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
                    f"Calendars {sorted(outside_pool)} are not in the pool of slot {slot.name!r}."
                )
        return selections_by_slot_id

    def _collect_owners_by_calendar(
        self, selected_calendar_ids: Iterable[int]
    ) -> dict[int, set[int]]:
        """Map each selected calendar's id → set of owner user ids."""
        selected_calendar_ids = list(selected_calendar_ids)
        if not selected_calendar_ids:
            return {}
        # Resolve owners via membership; orphan ownerships (null membership) are
        # intentionally excluded from the owner set.
        rows = (
            CalendarOwnership.objects.filter_by_organization(self.organization.id)
            .filter(
                calendar_fk_id__in=selected_calendar_ids,
                membership_user_id__isnull=False,
            )
            .values_list("calendar_fk_id", "membership_user_id")
        )
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

    def _assert_calendars_within_group_scoped_windows(
        self,
        slot_calendar_pairs: Iterable[tuple[int, int]],
        start: datetime.datetime,
        end: datetime.datetime,
    ) -> None:
        """Reject any ``(slot_id, calendar_id)`` pair whose calendar has
        group-scoped availability windows configured for that slot but
        ``[start, end)`` is not fully covered by any of them (spec Acceptance 4,
        UC-4: a caller cannot book, by naming the calendar directly, a time
        discovery would never have offered).

        Mirrors ``slot_engine.calendar_free_for_window``'s intersection exactly:
        a calendar with NO group-scoped window for a given ``(calendar, slot)``
        pair falls through untouched -- narrowing only ever narrows what was
        actually configured. Self-gating -- the existence check below is the
        only extra query when nothing is configured for any of the named
        pairs; the (fixed-cost) expanded fetch only runs when at least one pair
        IS configured. Called from both ``create_grouped_event`` (after the
        base-availability check) and ``reschedule_grouped_event`` (spec: every
        enforcement surface agrees).
        """
        pairs = list(slot_calendar_pairs)
        if not pairs:
            return

        org_id = cast(Organization, self.organization).id
        slot_ids = {slot_id for slot_id, _ in pairs}
        calendar_ids = {cid for _, cid in pairs}

        configured_pairs = set(
            AvailableTime.objects.unscoped()
            .filter_by_organization(org_id)
            .filter(group_slot_fk_id__in=slot_ids, calendar_fk_id__in=calendar_ids)
            .values_list("group_slot_fk_id", "calendar_fk_id")
            .distinct()
        )
        if not configured_pairs:
            return

        configured_slot_ids = {slot_id for slot_id, _ in configured_pairs}
        configured_calendar_ids = {cid for _, cid in configured_pairs}
        spans_by_slot = slot_engine.fetch_group_scoped_available_spans(
            org_id, configured_slot_ids, configured_calendar_ids, start, end
        )

        for slot_id, calendar_id in pairs:
            if (slot_id, calendar_id) not in configured_pairs:
                continue
            spans = spans_by_slot.get(slot_id, {}).get(calendar_id, ())
            if not slot_engine.window_fully_covered_by_spans(spans, start, end):
                raise CalendarGroupScopedRuleViolationError(calendar_id=calendar_id)

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
                start_time_tz_unaware=_convert_naive_utc_datetime_to_timezone(start_time, tz),
                end_time_tz_unaware=_convert_naive_utc_datetime_to_timezone(end_time, tz),
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
        now: datetime.datetime | None = None,
    ) -> list[BookableSlotProposal]:
        """Return every `(candidate_start, candidate_start + duration)` within
        `[search_window_start, search_window_end]`, stepping by `slot_step`,
        where every slot in the group has at least `required_count` calendars
        available, filtered by the resolved group booking policy.

        The implementation fetches blocking data (AvailableTime for managed
        calendars, CalendarEvent + BlockedTime for unmanaged calendars) once
        for the whole search window and then walks candidates in Python — one
        query per type instead of one query per candidate. For a 24h window at
        15-minute steps that turns 96 round-trips into 3, which is the core of
        the SQL generate_series optimization.

        Set `with_bulk_modifications=True` to expand recurring events through
        their bulk-modification continuation series.

        ``now`` defaults to ``timezone.now()`` (the request instant) and is used
        for lead-time / max-horizon cutoffs.  The GraphQL resolvers keep calling
        this method without the ``now`` argument (default accepted) — no signature
        change at the GraphQL layer.

        Policy-awareness check: when no ``BookingPolicy`` resolves for the group
        (i.e. the resolved policy is ``EffectivePolicy.unconstrained()``), the
        output is byte-for-byte identical to the pre-feature engine result — no
        buffer fetch, no filter applied.

        Buffer suppression semantics: when a buffer policy applies, a candidate
        is dropped if ANY participant calendar (across all slot pools, regardless
        of ``required_count``) has an event within the buffer dead zone.  This is
        the conservative "reject if any participant would reject" rule — even a
        calendar that is not counted toward a slot's ``required_count`` can block
        the candidate.  The intent is to never offer a slot that a participant
        would individually reject.

        Group-scoped availability windows (``CALENDAR_GROUP_SCOPED_AVAILABILITY``
        Phase 1b) are intersected in AFTER base availability, before the policy
        filter: a calendar with no group-scoped window configured for its slot
        is unaffected (fall-through default, zero added queries -- see
        ``_slot_pools_with_group_scoped_flags``); a calendar WITH one is only
        counted toward its slot's ``required_count`` when the candidate window
        is fully covered by at least one of its group-scoped windows --
        narrowing only, never widening base availability.
        """
        self._assert_initialized()
        if slot_step <= datetime.timedelta(0):
            raise CalendarGroupValidationError("slot_step must be a positive timedelta.")
        if duration <= datetime.timedelta(0):
            raise CalendarGroupValidationError("duration must be a positive timedelta.")

        if now is None:
            now = timezone.now()

        group = self._get_group_by_id(group_id)
        slots = list(group.slots.all())
        if not slots:
            return []

        slot_pool_by_id, group_scoped_calendar_ids_by_slot = (
            self._slot_pools_with_group_scoped_flags(slots)
        )
        required_count_by_slot_id = {s.id: s.required_count for s in slots}

        all_calendar_ids: set[int] = set()
        for ids in slot_pool_by_id.values():
            all_calendar_ids.update(ids)
        if not all_calendar_ids:
            return []

        managed_ids, unmanaged_ids = slot_engine.split_calendars_by_management(
            self.organization.id, all_calendar_ids
        )
        available_spans = slot_engine.fetch_available_spans(
            self.organization.id, managed_ids, search_window_start, search_window_end
        )
        blocking_spans = slot_engine.fetch_blocking_spans(
            self.organization.id,
            unmanaged_ids,
            search_window_start,
            search_window_end,
            with_bulk_modifications=with_bulk_modifications,
        )

        # ------------------------------------------------------------------
        # Group-scoped availability windows (CALENDAR_GROUP_SCOPED_AVAILABILITY
        # Phase 1b) -- self-gating early-out.
        # ------------------------------------------------------------------
        # `group_scoped_calendar_ids_by_slot` was already computed above by
        # folding an EXISTS() subquery into the per-slot membership query that
        # already ran -- zero added round trips. Only when at least one
        # calendar anywhere in the group actually has a group-scoped window
        # configured do we pay for the (fixed, non-per-candidate) expanded
        # fetch below.
        group_scoped_spans_by_slot: slot_engine.GroupScopedSpansBySlot = {}
        if group_scoped_calendar_ids_by_slot:
            configured_slot_ids = list(group_scoped_calendar_ids_by_slot.keys())
            configured_calendar_ids: set[int] = set()
            for ids in group_scoped_calendar_ids_by_slot.values():
                configured_calendar_ids.update(ids)
            group_scoped_spans_by_slot = slot_engine.fetch_group_scoped_available_spans(
                self.organization.id,
                configured_slot_ids,
                configured_calendar_ids,
                search_window_start,
                search_window_end,
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
                    if slot_engine.calendar_free_for_window(
                        cid,
                        window_start,
                        window_end,
                        managed_ids,
                        available_spans,
                        blocking_spans,
                        group_scoped_calendar_ids_by_slot.get(slot_id),
                        group_scoped_spans_by_slot.get(slot_id),
                    ):
                        available_count += 1
                if available_count < required_count_by_slot_id[slot_id]:
                    all_slots_satisfied = False
                    break
            if all_slots_satisfied:
                proposals.append(BookableSlotProposal(start_time=window_start, end_time=window_end))
            cursor = cursor + slot_step

        # ------------------------------------------------------------------
        # Policy filter — gated by data-presence
        # ------------------------------------------------------------------
        # When no booking_policy_service is injected (e.g. legacy test fixtures
        # that instantiate CalendarGroupService directly without DI), or when the
        # resolved policy is unconstrained (no BookingPolicy anywhere for this
        # group), skip ALL policy work so the output is byte-for-byte the
        # pre-feature engine result.
        if self.booking_policy_service is None:
            return proposals

        # _assert_initialized() already ran above; org is guaranteed non-None here.
        org_id = cast(Organization, self.organization).id

        policy = self.booking_policy_service.resolve_for_group(group)
        if policy == EffectivePolicy.unconstrained():
            return proposals

        # A buffer applies → fetch blocking spans for ALL participant calendars
        # (managed included), mirroring BookableSlotsService._buffer_blocking_spans.
        # Window widened: start side by buffer_after, end side by buffer_before
        # (so spans just outside the search window can still clip a candidate via
        # their dead zone — see slot_engine module docstring).
        no_buffer = policy.buffer_before <= datetime.timedelta(0) and policy.buffer_after <= (
            datetime.timedelta(0)
        )
        if no_buffer:
            buffer_blocking_spans: slot_engine.SpansByCalendarId = {}
        else:
            buffer_blocking_spans = slot_engine.fetch_blocking_spans(
                org_id,
                all_calendar_ids,
                search_window_start - policy.buffer_after,
                search_window_end + policy.buffer_before,
                with_bulk_modifications=with_bulk_modifications,
            )

        return slot_engine.apply_policy_filter(proposals, policy, now, buffer_blocking_spans)
