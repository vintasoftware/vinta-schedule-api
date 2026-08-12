import datetime
import uuid
from collections.abc import Iterable
from typing import TYPE_CHECKING, Annotated, cast

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, QuerySet
from django.utils import timezone

from dependency_injector.wiring import Provide, inject

from audit.constants import AuditAction
from audit.diff import compute_diff
from calendar_integration.constants import (
    CalendarProvider,
    CalendarType,
    GroupScopedRuleType,
    QuotaPeriod,
)
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
    CalendarGroupSlotQuotaRule,
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
    GroupScopedBlockWriteResult,
    ResourceAllocationInputData,
)
from payments.billing_constants import LimitedResource
from payments.exceptions import OverLimitError
from tenancy.models import Organization
from users.models import User


if TYPE_CHECKING:
    from audit.services import AuditService
    from calendar_integration.services.booking_policy_service import BookingPolicyService
    from calendar_integration.services.calendar_service import CalendarService
    from payments.services.entitlement_service import EntitlementService
    from public_api.models import SystemUser


# Sentinel for partial updates: distinguishes "omit rrule_string" (leave the
# recurrence alone) from an explicit ``None`` (clear it, making the window
# non-recurring). Mirrors ``calendar_service._UNCHANGED``.
_UNCHANGED = object()


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
    @staticmethod
    def _is_quota_uniqueness_constraint_violation(e: IntegrityError) -> bool:
        """Check if an IntegrityError is the (group_slot, calendar, period) unique
        constraint violation on CalendarGroupSlotQuotaRule.

        Returns True if the error message contains the unique constraint name,
        False otherwise. Non-uniqueness constraint violations should be re-raised,
        not converted to a validation error.
        """
        constraint_name = "calendargroupslotquotarule_unique_slot_calendar_period"
        return constraint_name in str(e)

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

    def _delete_group_scoped_rows_for_removed_calendars(
        self,
        queryset: "QuerySet[AvailableTime] | QuerySet[BlockedTime] | QuerySet[CalendarGroupSlotQuotaRule]",
    ) -> None:
        """Audit-then-delete every row in ``queryset`` (already filtered to one
        slot and a set of removed calendar ids).

        Shared by ``_reconcile_slot`` for group-scoped windows
        (``AvailableTime``), group-scoped blocked time (``BlockedTime``,
        Phase 2a), and quota rules (``CalendarGroupSlotQuotaRule``, Phase 3a)
        -- same cleanup shape, different model. No-op (still issues the
        delete) when no ``audit_service`` is bound.
        """
        rows = list(queryset)
        for row in rows:
            if self.audit_service is None or self.organization is None:
                break
            user_or_token = getattr(self.calendar_service, "user_or_token", None)
            permission_service = getattr(self.calendar_service, "calendar_permission_service", None)
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
                subject=self.audit_service.subject_from_instance(row),
            )
        queryset.delete()

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

            # Delete group-scoped windows, blocked time, and quota rules for the
            # removed calendars. The FK on AvailableTime.group_slot /
            # BlockedTime.group_slot / CalendarGroupSlotQuotaRule.group_slot →
            # CalendarGroupSlot cascades on SLOT deletion only, not on
            # membership removal, so we must explicitly clean up the orphaned
            # rows here. Each row deletion is audited individually because the
            # group-update diff only captures name/description/
            # accepts_public_scheduling, not membership or window/block/quota
            # changes.
            org_id = cast(Organization, self.organization).id
            self._delete_group_scoped_rows_for_removed_calendars(
                AvailableTime.objects.unscoped()
                .filter_by_organization(org_id)
                .filter(group_slot_fk=slot, calendar_fk_id__in=to_remove)
            )
            self._delete_group_scoped_rows_for_removed_calendars(
                BlockedTime.objects.unscoped()
                .filter_by_organization(org_id)
                .filter(group_slot_fk=slot, calendar_fk_id__in=to_remove)
            )
            self._delete_group_scoped_rows_for_removed_calendars(
                CalendarGroupSlotQuotaRule.objects.filter_by_organization(org_id).filter(
                    group_slot_fk=slot, calendar_fk_id__in=to_remove
                )
            )

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
        acting_user: "User | SystemUser",
        subject_instance: AvailableTime,
        diff: dict | None = None,
    ) -> None:
        """Emit an audit record for a group-scoped availability window write.

        Unlike ``_audit_group_write`` (which resolves the actor from a bound
        ``calendar_service``'s auth context), these write methods take the
        acting principal explicitly -- they are reachable without a bound
        ``calendar_service``. No-op when no ``audit_service`` / ``organization``
        is bound, so instrumentation never breaks a write path.

        ``acting_user`` accepts a ``SystemUser`` too (public-API batch write,
        CALENDAR_GROUP_SCOPED_AVAILABILITY Phase 1d) --
        ``AuditService.actor_from_user_or_token`` already resolves either.
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
        same batched implementation the discovery-side fetch uses. Occurrence
        expansion for group-scoped masters is safe because (a) no write path
        creates a group-scoped recurrence exception yet, and (b)
        ``RecurringMixin._get_occurrences_in_range`` now routes the
        exception-instance lookup through ``_base_manager`` when the master is
        group-scoped, ensuring group-scoped exception rows are found if one ever
        becomes reachable.
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
        self._check_not_restricted()
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
        rrule_string: str | None = _UNCHANGED,  # type: ignore[assignment]
        now: datetime.datetime | None = None,
    ) -> GroupScopedAvailabilityWriteResult:
        """Partially update a group-scoped availability window (only provided
        fields change -- mirrors ``AvailabilityService.update_blocked_time``).

        ``rrule_string`` is tri-state: the sentinel ``_UNCHANGED`` (the default)
        leaves the recurrence untouched, explicit ``None`` clears it (the window
        becomes non-recurring), and a string sets/replaces it.

        After the update is applied, every confirmed future booking in the
        window's group slot for its calendar that no longer falls inside the
        calendar's group-scoped configuration is collected and returned.
        Narrowing a window never cancels or edits a booking (spec UC-6) --
        the caller decides what to do with each one.
        """
        self._assert_initialized()
        self._check_not_restricted()
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
        if rrule_string is not _UNCHANGED:
            # ``None`` clears the recurrence (non-recurring); a string sets/replaces it.
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
            calendar_id=cast(int, window.calendar_fk_id),
            group_slot=cast(CalendarGroupSlot, window.group_slot),
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
        self._check_not_restricted()
        window = self._get_group_scoped_window(window_id)
        self._authorize_group_scoped_write(acting_user, window.calendar, window.group_slot)

        self._audit_group_scoped_availability_write(AuditAction.DELETE, acting_user, window)
        window.delete()

    def _find_matching_group_scoped_window(
        self,
        group_slot_id: int,
        calendar_id: int,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        tz: str,
        rrule_string: str | None,
    ) -> AvailableTime | None:
        """Find an existing group-scoped window with identical content, if any.

        Backs the batch-upsert write path's idempotent ``create``
        (CALENDAR_GROUP_SCOPED_AVAILABILITY Phase 1d): an upstream system
        that resends the same batch after a network timeout (spec UC-5) must
        land on the identical final state, not accumulate duplicate rows.
        Matched on (calendar, group slot, start_time, end_time, timezone,
        rrule). ``start_time``/``end_time`` are compared as the aware
        instants they are -- paired with an exact ``timezone`` match this is
        a safe point lookup, not the cross-timezone comparison
        ``start_time_tz_unaware`` is unsafe for (see AGENTS.md).
        """
        organization = cast(Organization, self.organization)
        candidates = (
            AvailableTime.objects.for_group_slot(group_slot_id)
            .filter_by_organization(organization.id)
            .filter(
                calendar_fk_id=calendar_id,
                start_time_tz_unaware=start_time,
                end_time_tz_unaware=end_time,
                timezone=tz,
            )
            .select_related("recurrence_rule")
        )
        for candidate in candidates:
            candidate_rrule = (
                candidate.recurrence_rule.to_rrule_string() if candidate.recurrence_rule else None
            )
            if candidate_rrule == rrule_string:
                return candidate
        return None

    @transaction.atomic()
    def batch_upsert_group_scoped_availability_windows(
        self,
        group_slot_id: int,
        operations: Iterable[dict],
        acting_principal: "User | SystemUser",
    ) -> list[AvailableTime]:
        """Apply an atomic bulk-upsert batch of group-scoped availability
        window create/update/delete operations within one group slot's
        roster (CALENDAR_GROUP_SCOPED_AVAILABILITY Phase 1d -- public API).

        Mirrors ``AvailabilityService.batch_modify_available_times``'s
        transaction / entitlement / net-growth structure so the two batch
        contracts read the same way to an integration:

        * **All-or-nothing.** The whole batch runs inside one transaction
          (this method's own ``@transaction.atomic()``, nested inside the
          request's ``ATOMIC_REQUESTS`` transaction); every operation is
          validated and every touched row is resolved *before* any write, so
          a bad ``window_id`` or a batch that would exceed the plan's
          ``availability_windows`` ceiling rolls back cleanly with nothing
          partially applied.
        * **Idempotent create.** A ``create`` operation is an upsert, not a
          blind insert: :meth:`_find_matching_group_scoped_window` looks for
          an existing group-scoped window with identical content first. A
          match is returned unchanged (no write, no entitlement charge, no
          audit record) rather than duplicated, so replaying an identical
          batch (spec UC-5: "the system replays the same batch after a
          network timeout") lands on the exact same final state.
        * **update / delete** require ``window_id`` and act on exactly the
          row it names, like the base availability batch write's ``id``-keyed
          operations.

        Authorization is split between the CALLER and this method. Unlike the
        single-window Phase 1a writes above, this method does not call
        ``CalendarPermissionService`` -- every public-API availability write
        authorizes via ``OrganizationResourceAccess`` (token resource grant)
        plus owner-scope (``public_api.scoping.assert_calendar_in_owner_scope``),
        never the human-facing per-membership permission check, because a
        public-API ``SystemUser`` token has no ``CalendarOwnership``-linked
        ``User`` to check it against for an org-wide token. The caller's
        owner-scope guard only proves the token owns each op's
        ``calendar_id`` -- it says nothing about which calendar an
        update/delete op's ``window_id`` actually belongs to. This method
        closes that gap itself: for every update/delete op it cross-checks
        the resolved window's ``calendar_fk_id`` against that op's own
        ``calendar_id`` and rejects the whole batch (not-found-shaped, see
        ``:raises CalendarGroupSlotConfigNotFoundError:`` below) if they
        don't match, so a token cannot use a calendar it owns to reach a
        window that belongs to a different calendar. ``acting_principal`` is
        used for audit attribution only.

        :param operations: dicts with ``action`` (create/update/delete) and
            ``calendar_id``; create/update also take ``start_time``,
            ``end_time``, ``timezone``, ``rrule_string`` (optional); update/
            delete also take ``window_id``.
        :param acting_principal: the ``User`` or public-API ``SystemUser``
            this write is attributed to in the audit trail.
        :raises OverLimitError: when the batch's net growth (genuine creates
            minus credited deletes) would take the organization past its
            effective ``availability_windows`` ceiling. Nothing is applied.
        :raises CalendarGroupSlotConfigNotFoundError: a create op's
            ``calendar_id`` is not a member of ``group_slot_id``'s roster, or
            an update/delete op's ``window_id`` does not resolve to a
            group-scoped window in this slot, or that window does not belong
            to the op's own ``calendar_id``.
        :raises ValueError: an operation's shape is invalid (unknown action,
            missing required field).
        :return: every group-scoped window in ``group_slot_id``'s roster
            (all calendars) after the batch is applied.
        """
        self._assert_initialized()
        organization = cast(Organization, self.organization)
        operations = list(operations)

        valid_actions = {"create", "update", "delete"}
        for op in operations:
            action = op.get("action")
            if action not in valid_actions:
                raise ValueError(f"Invalid operation action: {action}")
            if action == "create" and (
                "calendar_id" not in op
                or "start_time" not in op
                or "end_time" not in op
                or "timezone" not in op
            ):
                raise ValueError(
                    "create operation requires calendar_id, start_time, end_time, timezone"
                )
            if action in ("update", "delete") and "window_id" not in op:
                raise ValueError(f"{action} operation requires window_id")

        # RESTRICTED must block the batch outright, including an update-only
        # or delete-only batch -- mirrors batch_modify_available_times.
        if self.entitlement_service is not None and operations:
            self.entitlement_service.check_not_restricted(organization)

        # Lock the billing root BEFORE _find_matching_group_scoped_window's
        # idempotent-create content-match reads (and before the delete-credit
        # read further below) -- not just before check_limit, like
        # batch_modify_available_times's later lock does. The content-match is
        # itself a read whose "no identical window exists yet" answer a
        # concurrent replay of the same batch (spec UC-5) can invalidate: two
        # genuinely concurrent identical batches would otherwise both see no
        # match, both create, and double-charge the entitlement. Taking the
        # lock this early serializes them so the second sees the first's
        # committed windows and correctly no-ops.
        if self.entitlement_service is not None and operations:
            self.entitlement_service.lock_billing_root(organization)

        # Resolve every touched row up front (read-only): a missing/foreign
        # window_id or a calendar not on this slot's roster fails the whole
        # batch before anything is written.
        windows_by_op_id: dict[int, AvailableTime] = {}
        for op in operations:
            if op["action"] in ("update", "delete"):
                window = self._get_group_scoped_window(op["window_id"])
                if window.group_slot_fk_id != group_slot_id:
                    raise CalendarGroupSlotConfigNotFoundError()
                # Cross-check the resolved window against the op's OWN calendar_id --
                # public_api's assert_calendar_in_owner_scope only proves the token owns
                # op["calendar_id"], not that window_id actually belongs to it. Without
                # this, a calendar-owner-scoped token could target another calendar's
                # window in the same slot by pairing a calendar_id it owns with a
                # window_id it doesn't. Raises the same non-disclosure exception as an
                # unresolvable window_id so the two cases are indistinguishable.
                if window.calendar_fk_id != op["calendar_id"]:
                    raise CalendarGroupSlotConfigNotFoundError()
                windows_by_op_id[op["window_id"]] = window

        memberships_by_calendar: dict[int, CalendarGroupSlotMembership] = {}
        for op in operations:
            if op["action"] == "create" and op["calendar_id"] not in memberships_by_calendar:
                memberships_by_calendar[op["calendar_id"]] = self._resolve_group_scoped_membership(
                    group_slot_id, op["calendar_id"]
                )

        # Idempotent-create resolution: match each create op against an
        # existing group-scoped window before it counts toward net growth.
        matched_existing: dict[int, AvailableTime] = {}
        for i, op in enumerate(operations):
            if op["action"] != "create":
                continue
            existing = self._find_matching_group_scoped_window(
                group_slot_id=group_slot_id,
                calendar_id=op["calendar_id"],
                start_time=op["start_time"],
                end_time=op["end_time"],
                tz=op["timezone"],
                rrule_string=op.get("rrule_string"),
            )
            if existing is not None:
                matched_existing[i] = existing

        genuine_create_count = sum(
            1
            for i, op in enumerate(operations)
            if op["action"] == "create" and i not in matched_existing
        )
        delete_ids = [op["window_id"] for op in operations if op["action"] == "delete"]

        # The billing root is already locked above (before the idempotent-create
        # content-match), so the delete-credit read below is already covered --
        # no separate lock call needed here, unlike batch_modify_available_times.

        # Only deletions of rows the usage counter counts offset the creates.
        # Reads through ``unscoped()`` because group-scoped rows are invisible
        # to the default manager ``only_user_authored`` wraps.
        credited_delete_count = (
            AvailableTime.objects.unscoped()
            .filter_by_organization(organization.id)
            .only_user_authored()
            .filter(id__in=delete_ids)
            .count()
            if genuine_create_count and delete_ids
            else 0
        )
        delta = max(genuine_create_count - credited_delete_count, 0)

        if delta and self.entitlement_service is not None:
            result = self.entitlement_service.check_limit(
                organization, LimitedResource.AVAILABILITY_WINDOWS, delta=delta, lock=True
            )
            if not result.allowed:
                raise OverLimitError.from_check_result(result)

        for i, op in enumerate(operations):
            action = op["action"]
            if action == "create":
                if i in matched_existing:
                    continue
                membership = memberships_by_calendar[op["calendar_id"]]
                recurrence_rule = self._create_recurrence_rule_if_needed(op.get("rrule_string"))
                window = AvailableTime.objects.unscoped().create(
                    organization=organization,
                    calendar=membership.calendar,
                    group_slot=membership.slot,
                    start_time_tz_unaware=op["start_time"],
                    end_time_tz_unaware=op["end_time"],
                    timezone=op["timezone"],
                    recurrence_rule=recurrence_rule,
                )
                self._audit_group_scoped_availability_write(
                    AuditAction.CREATE, acting_principal, window
                )
            elif action == "update":
                window = windows_by_op_id[op["window_id"]]
                before = {
                    "start_time_tz_unaware": window.start_time_tz_unaware.isoformat(),
                    "end_time_tz_unaware": window.end_time_tz_unaware.isoformat(),
                    "timezone": window.timezone,
                    "rrule": (
                        window.recurrence_rule.to_rrule_string() if window.recurrence_rule else None
                    ),
                }
                update_fields: list[str] = []
                if "start_time" in op:
                    window.start_time_tz_unaware = op["start_time"]
                    update_fields.append("start_time_tz_unaware")
                if "end_time" in op:
                    window.end_time_tz_unaware = op["end_time"]
                    update_fields.append("end_time_tz_unaware")
                if "timezone" in op:
                    window.timezone = op["timezone"]
                    update_fields.append("timezone")
                if "rrule_string" in op:
                    window.recurrence_rule = self._create_recurrence_rule_if_needed(
                        op["rrule_string"]
                    )
                    update_fields.append("recurrence_rule_fk")
                if update_fields:
                    window.save(update_fields=[*update_fields, "modified"])
                after = {
                    "start_time_tz_unaware": window.start_time_tz_unaware.isoformat(),
                    "end_time_tz_unaware": window.end_time_tz_unaware.isoformat(),
                    "timezone": window.timezone,
                    "rrule": (
                        window.recurrence_rule.to_rrule_string() if window.recurrence_rule else None
                    ),
                }
                self._audit_group_scoped_availability_write(
                    AuditAction.UPDATE, acting_principal, window, diff=compute_diff(before, after)
                )
            elif action == "delete":
                window = windows_by_op_id[op["window_id"]]
                self._audit_group_scoped_availability_write(
                    AuditAction.DELETE, acting_principal, window
                )
                window.delete()

        return list(
            AvailableTime.objects.for_group_slot(group_slot_id)
            .filter_by_organization(organization.id)
            .select_related("recurrence_rule")
            .order_by("pk")
        )

    # ------------------------------------------------------------------
    # Group-scoped blocked time (Phase 2a of
    # CALENDAR_GROUP_SCOPED_AVAILABILITY -- writes)
    # ------------------------------------------------------------------

    def _get_group_scoped_block(self, block_id: int) -> BlockedTime:
        """Fetch a group-scoped ``BlockedTime`` row by id, scoped to this org.

        Reads through the ``unscoped`` accessor (never the default manager,
        which excludes group-scoped rows) and requires ``group_slot`` to be
        set, so an id belonging to a base row raises the same not-found error
        as a genuinely missing id. Mirrors ``_get_group_scoped_window``.
        """
        org_id = cast(Organization, self.organization).id
        try:
            return (
                BlockedTime.objects.unscoped()
                .filter_by_organization(org_id)
                .select_related("group_slot", "calendar", "recurrence_rule")
                .get(id=block_id, group_slot_fk__isnull=False)
            )
        except BlockedTime.DoesNotExist:
            raise CalendarGroupSlotConfigNotFoundError() from None

    def _audit_group_scoped_block_write(
        self,
        action: str,
        acting_user: "User | SystemUser",
        subject_instance: BlockedTime,
        diff: dict | None = None,
    ) -> None:
        """Emit an audit record for a group-scoped blocked-time write.

        Mirrors ``_audit_group_scoped_availability_write``: the acting
        principal is taken explicitly (this write path is reachable without a
        bound ``calendar_service``), and this is a no-op when no
        ``audit_service`` / ``organization`` is bound.
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

    def _group_scoped_blocked_times_expanded(
        self,
        calendar_id: int,
        group_slot_id: int,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[BlockedTime]:
        """Expand every group-scoped ``BlockedTime`` for ``(calendar, group_slot)``
        that overlaps ``[start_date, end_date)``, recurrence included.

        Mirrors ``_group_scoped_available_times_expanded``, delegating to
        ``slot_engine.expand_group_scoped_blocked_times`` -- the single-pair
        case of the same batched implementation the discovery-side fetch
        uses.
        """
        org_id = cast(Organization, self.organization).id
        return slot_engine.expand_group_scoped_blocked_times(
            org_id, [group_slot_id], [calendar_id], start_date, end_date
        )

    def _find_bookings_orphaned_by_group_scoped_block(
        self, calendar_id: int, group_slot: CalendarGroupSlot, now: datetime.datetime
    ) -> list[CalendarEvent]:
        """Confirmed future bookings in ``group_slot`` for ``calendar_id`` that
        fall INSIDE the calendar's current group-scoped blocked time, after a
        block write.

        The block analog of ``_find_orphaned_bookings``, with the coverage
        test inverted: a window write orphans a booking that falls OUTSIDE the
        configured union of windows (the window no longer grants that time); a
        block write orphans a booking that falls INSIDE any configured block
        (the block now removes that time). Runs against every group-scoped
        block currently configured for this ``(calendar, slot)`` pair, not
        just the one a caller just wrote, for the same reason windows do.
        Nothing here is cancelled or modified; this is purely a read (spec
        UC-6's rule applied to blocks).
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

        # Expand ALL group-scoped blocks once over the union range.
        all_blocks = self._group_scoped_blocked_times_expanded(
            calendar_id, group_slot.id, min_start, max_end
        )

        # Check each booking against the single cached expansion.
        orphaned: list[CalendarEvent] = []
        for selection in selections_list:
            event = selection.event
            overlaps_block = any(
                slot_engine.intervals_overlap(
                    (block.start_time, block.end_time), (event.start_time, event.end_time)
                )
                for block in all_blocks
            )
            if overlaps_block:
                orphaned.append(event)
        return orphaned

    @transaction.atomic()
    def create_group_scoped_blocked_time(
        self,
        acting_user: User,
        group_slot_id: int,
        calendar_id: int,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        tz: str,
        reason: str = "",
        rrule_string: str | None = None,
        now: datetime.datetime | None = None,
    ) -> GroupScopedBlockWriteResult:
        """Create a group-scoped blocked time for ``(calendar, group_slot)``.

        Writes through the explicit group-scoped accessor
        (``BlockedTime.objects.unscoped().create(..., group_slot=...)``),
        never the default manager. Carries the same recurrence expressiveness
        as a base ``BlockedTime`` -- pass ``rrule_string`` for a recurring
        block. Permission-gated identically to windows: ``acting_user`` must
        own the calendar or be an org admin (see
        ``CalendarPermissionService.can_manage_group_scoped_calendar_config``).

        Unlike a group-scoped WINDOW -- where windows OR together, so only the
        FIRST one flips a calendar from fall-through to narrowed and can
        orphan a booking -- every group-scoped BLOCK independently subtracts
        time from whatever was previously offered. So orphaned-booking
        detection runs on EVERY create, not just the first (spec UC-6's rule
        applied to blocks). Nothing is cancelled.
        """
        self._assert_initialized()
        self._check_not_restricted()
        if now is None:
            now = timezone.now()

        organization = cast(Organization, self.organization)
        membership = self._resolve_group_scoped_membership(group_slot_id, calendar_id)
        self._authorize_group_scoped_write(acting_user, membership.calendar, membership.slot)

        recurrence_rule = self._create_recurrence_rule_if_needed(rrule_string)
        block = BlockedTime.objects.unscoped().create(
            organization=organization,
            calendar=membership.calendar,
            group_slot=membership.slot,
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
            timezone=tz,
            reason=reason,
            # BlockedTime enforces uniqueness on (calendar, external_id); this
            # write path has no natural external id (unlike provider-synced
            # blocks), so a uuid4 guarantees no collision with any other block
            # -- base or group-scoped -- on the same calendar.
            external_id=f"group-scoped-block-{uuid.uuid4()}",
            recurrence_rule=recurrence_rule,
        )
        self._audit_group_scoped_block_write(AuditAction.CREATE, acting_user, block)

        orphaned_bookings = self._find_bookings_orphaned_by_group_scoped_block(
            calendar_id=calendar_id,
            group_slot=membership.slot,
            now=now,
        )

        return GroupScopedBlockWriteResult(block=block, orphaned_bookings=orphaned_bookings)

    @transaction.atomic()
    def update_group_scoped_blocked_time(
        self,
        acting_user: User,
        block_id: int,
        start_time: datetime.datetime | None = None,
        end_time: datetime.datetime | None = None,
        tz: str | None = None,
        reason: str | None = None,
        rrule_string: str | None = _UNCHANGED,  # type: ignore[assignment]
        now: datetime.datetime | None = None,
    ) -> GroupScopedBlockWriteResult:
        """Partially update a group-scoped blocked time (only provided fields
        change -- mirrors ``update_group_scoped_availability_window``).

        ``rrule_string`` is tri-state: the sentinel ``_UNCHANGED`` (the
        default) leaves the recurrence untouched, explicit ``None`` clears it
        (the block becomes non-recurring), and a string sets/replaces it.

        After the update is applied, every confirmed future booking in the
        block's group slot for its calendar that now falls INSIDE the
        calendar's group-scoped blocked time is collected and returned.
        Changing a block never cancels or edits a booking (spec UC-6's rule
        applied to blocks) -- the caller decides what to do with each one.
        """
        self._assert_initialized()
        self._check_not_restricted()
        if now is None:
            now = timezone.now()

        block = self._get_group_scoped_block(block_id)
        self._authorize_group_scoped_write(acting_user, block.calendar, block.group_slot)

        before = {
            "start_time_tz_unaware": block.start_time_tz_unaware.isoformat(),
            "end_time_tz_unaware": block.end_time_tz_unaware.isoformat(),
            "timezone": block.timezone,
            "reason": block.reason,
            "rrule": block.recurrence_rule.to_rrule_string() if block.recurrence_rule else None,
        }

        update_fields: list[str] = []
        if start_time is not None:
            block.start_time_tz_unaware = start_time
            update_fields.append("start_time_tz_unaware")
        if end_time is not None:
            block.end_time_tz_unaware = end_time
            update_fields.append("end_time_tz_unaware")
        if tz is not None:
            block.timezone = tz
            update_fields.append("timezone")
        if reason is not None:
            block.reason = reason
            update_fields.append("reason")
        if rrule_string is not _UNCHANGED:
            # ``None`` clears the recurrence (non-recurring); a string sets/replaces it.
            block.recurrence_rule = self._create_recurrence_rule_if_needed(rrule_string)
            # Assigning through the ForeignObject property name ("recurrence_rule")
            # sets the underlying concrete column ("recurrence_rule_fk"); `save`'s
            # `update_fields` must name the concrete field.
            update_fields.append("recurrence_rule_fk")

        if update_fields:
            block.save(update_fields=[*update_fields, "modified"])

        after = {
            "start_time_tz_unaware": block.start_time_tz_unaware.isoformat(),
            "end_time_tz_unaware": block.end_time_tz_unaware.isoformat(),
            "timezone": block.timezone,
            "reason": block.reason,
            "rrule": block.recurrence_rule.to_rrule_string() if block.recurrence_rule else None,
        }
        self._audit_group_scoped_block_write(
            AuditAction.UPDATE, acting_user, block, diff=compute_diff(before, after)
        )

        orphaned_bookings = self._find_bookings_orphaned_by_group_scoped_block(
            calendar_id=cast(int, block.calendar_fk_id),
            group_slot=cast(CalendarGroupSlot, block.group_slot),
            now=now,
        )
        return GroupScopedBlockWriteResult(block=block, orphaned_bookings=orphaned_bookings)

    @transaction.atomic()
    def delete_group_scoped_blocked_time(self, acting_user: User, block_id: int) -> None:
        """Delete a group-scoped blocked time (a single ``BlockedTime`` row).

        A recurring block is stored as one row; deleting it removes the whole
        series (mirrors ``delete_group_scoped_availability_window``). No
        orphaned-booking report is computed here -- deleting a block only
        WIDENS available time (the inverse of a window delete narrowing it),
        so it can never orphan a booking. Removing a calendar from a slot's
        roster entirely (which cascades away its blocks per the Phase 0
        schema constraint) is a distinct action, exercised through
        ``update_group``.
        """
        self._assert_initialized()
        self._check_not_restricted()
        block = self._get_group_scoped_block(block_id)
        self._authorize_group_scoped_write(acting_user, block.calendar, block.group_slot)

        self._audit_group_scoped_block_write(AuditAction.DELETE, acting_user, block)
        block.delete()

    def _find_matching_group_scoped_block(
        self,
        group_slot_id: int,
        calendar_id: int,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        tz: str,
        reason: str,
        rrule_string: str | None,
    ) -> BlockedTime | None:
        """Find an existing group-scoped block with identical content, if any.

        Backs the batch-upsert write path's idempotent ``create`` (mirrors
        ``_find_matching_group_scoped_window``): an upstream system that
        resends the same batch after a network timeout (spec UC-5) must land
        on the identical final state, not accumulate duplicate rows. Matched
        on (calendar, group slot, start_time, end_time, timezone, reason,
        rrule). ``start_time``/``end_time`` are compared as the aware
        instants they are -- paired with an exact ``timezone`` match this is
        a safe point lookup, not the cross-timezone comparison
        ``start_time_tz_unaware`` is unsafe for (see AGENTS.md).
        """
        organization = cast(Organization, self.organization)
        candidates = (
            BlockedTime.objects.for_group_slot(group_slot_id)
            .filter_by_organization(organization.id)
            .filter(
                calendar_fk_id=calendar_id,
                start_time_tz_unaware=start_time,
                end_time_tz_unaware=end_time,
                timezone=tz,
                reason=reason,
            )
            .select_related("recurrence_rule")
        )
        for candidate in candidates:
            candidate_rrule = (
                candidate.recurrence_rule.to_rrule_string() if candidate.recurrence_rule else None
            )
            if candidate_rrule == rrule_string:
                return candidate
        return None

    @transaction.atomic()
    def batch_upsert_group_scoped_blocked_times(
        self,
        group_slot_id: int,
        operations: Iterable[dict],
        acting_principal: "User | SystemUser",
    ) -> list[BlockedTime]:
        """Apply an atomic bulk-upsert batch of group-scoped blocked-time
        create/update/delete operations within one group slot's roster
        (CALENDAR_GROUP_SCOPED_AVAILABILITY Phase 2b -- public API).

        Mirrors ``batch_upsert_group_scoped_availability_windows``'s
        transaction / idempotent-create / IDOR-cross-check structure exactly,
        with ONE deliberate difference: blocked time is not metered yet (that
        is Phase 2c), so this method never calls ``check_limit`` and never
        locks the billing root -- there is no entitlement delta to protect a
        lock against. It still calls ``check_not_restricted`` up front, like
        every other guarded write, so a ``RESTRICTED`` organization cannot
        write group-scoped blocks either.

        * **All-or-nothing.** The whole batch runs inside one transaction
          (this method's own ``@transaction.atomic()``, nested inside the
          request's ``ATOMIC_REQUESTS`` transaction); every operation is
          validated and every touched row is resolved *before* any write, so
          a bad ``block_id`` rolls back cleanly with nothing partially
          applied.
        * **Idempotent create.** A ``create`` operation is an upsert, not a
          blind insert: :meth:`_find_matching_group_scoped_block` looks for
          an existing group-scoped block with identical content first. A
          match is returned unchanged (no write, no audit record) rather
          than duplicated, so replaying an identical batch (spec UC-5) lands
          on the exact same final state.
        * **update / delete** require ``block_id`` and act on exactly the
          row it names, like the window batch write's ``id``-keyed
          operations.

        Authorization is split between the CALLER and this method, exactly
        like the window batch: the caller proves each op's ``calendar_id``
        is within the token's owner scope; this method cross-checks that an
        update/delete op's resolved block actually belongs to that op's own
        ``calendar_id`` and rejects the whole batch (not-found-shaped) if
        they don't match. ``acting_principal`` is used for audit attribution
        only.

        :param operations: dicts with ``action`` (create/update/delete) and
            ``calendar_id``; create/update also take ``start_time``,
            ``end_time``, ``timezone``, ``reason`` (optional), ``rrule_string``
            (optional); update/delete also take ``block_id``.
        :param acting_principal: the ``User`` or public-API ``SystemUser``
            this write is attributed to in the audit trail.
        :raises CalendarGroupSlotConfigNotFoundError: a create op's
            ``calendar_id`` is not a member of ``group_slot_id``'s roster, or
            an update/delete op's ``block_id`` does not resolve to a
            group-scoped block in this slot, or that block does not belong
            to the op's own ``calendar_id``.
        :raises ValueError: an operation's shape is invalid (unknown action,
            missing required field).
        :return: every group-scoped block in ``group_slot_id``'s roster (all
            calendars) after the batch is applied.
        """
        self._assert_initialized()
        organization = cast(Organization, self.organization)
        operations = list(operations)

        valid_actions = {"create", "update", "delete"}
        for op in operations:
            action = op.get("action")
            if action not in valid_actions:
                raise ValueError(f"Invalid operation action: {action}")
            if action == "create" and (
                "calendar_id" not in op
                or "start_time" not in op
                or "end_time" not in op
                or "timezone" not in op
            ):
                raise ValueError(
                    "create operation requires calendar_id, start_time, end_time, timezone"
                )
            if action in ("update", "delete") and "block_id" not in op:
                raise ValueError(f"{action} operation requires block_id")

        # RESTRICTED must block the batch outright, including an update-only
        # or delete-only batch -- mirrors the window batch, minus the
        # limit/lock machinery that exists only to protect entitlement
        # counting, which blocks don't have yet (Phase 2c).
        if self.entitlement_service is not None and operations:
            self.entitlement_service.check_not_restricted(organization)

        # Resolve every touched row up front (read-only): a missing/foreign
        # block_id or a calendar not on this slot's roster fails the whole
        # batch before anything is written.
        blocks_by_op_id: dict[int, BlockedTime] = {}
        for op in operations:
            if op["action"] in ("update", "delete"):
                block = self._get_group_scoped_block(op["block_id"])
                if block.group_slot_fk_id != group_slot_id:
                    raise CalendarGroupSlotConfigNotFoundError()
                # Cross-check the resolved block against the op's OWN calendar_id --
                # public_api's assert_calendar_in_owner_scope only proves the token owns
                # op["calendar_id"], not that block_id actually belongs to it. Without
                # this, a calendar-owner-scoped token could target another calendar's
                # block in the same slot by pairing a calendar_id it owns with a
                # block_id it doesn't. Raises the same non-disclosure exception as an
                # unresolvable block_id so the two cases are indistinguishable.
                if block.calendar_fk_id != op["calendar_id"]:
                    raise CalendarGroupSlotConfigNotFoundError()
                blocks_by_op_id[op["block_id"]] = block

        memberships_by_calendar: dict[int, CalendarGroupSlotMembership] = {}
        for op in operations:
            if op["action"] == "create" and op["calendar_id"] not in memberships_by_calendar:
                memberships_by_calendar[op["calendar_id"]] = self._resolve_group_scoped_membership(
                    group_slot_id, op["calendar_id"]
                )

        # Idempotent-create resolution: match each create op against an
        # existing group-scoped block before writing anything.
        matched_existing: dict[int, BlockedTime] = {}
        for i, op in enumerate(operations):
            if op["action"] != "create":
                continue
            existing = self._find_matching_group_scoped_block(
                group_slot_id=group_slot_id,
                calendar_id=op["calendar_id"],
                start_time=op["start_time"],
                end_time=op["end_time"],
                tz=op["timezone"],
                reason=op.get("reason", ""),
                rrule_string=op.get("rrule_string"),
            )
            if existing is not None:
                matched_existing[i] = existing

        for i, op in enumerate(operations):
            action = op["action"]
            if action == "create":
                if i in matched_existing:
                    continue
                membership = memberships_by_calendar[op["calendar_id"]]
                recurrence_rule = self._create_recurrence_rule_if_needed(op.get("rrule_string"))
                block = BlockedTime.objects.unscoped().create(
                    organization=organization,
                    calendar=membership.calendar,
                    group_slot=membership.slot,
                    start_time_tz_unaware=op["start_time"],
                    end_time_tz_unaware=op["end_time"],
                    timezone=op["timezone"],
                    reason=op.get("reason", ""),
                    # BlockedTime enforces uniqueness on (calendar, external_id); this
                    # write path has no natural external id, so a uuid4 guarantees no
                    # collision with any other block -- base or group-scoped -- on the
                    # same calendar (mirrors create_group_scoped_blocked_time).
                    external_id=f"group-scoped-block-{uuid.uuid4()}",
                    recurrence_rule=recurrence_rule,
                )
                self._audit_group_scoped_block_write(AuditAction.CREATE, acting_principal, block)
            elif action == "update":
                block = blocks_by_op_id[op["block_id"]]
                before = {
                    "start_time_tz_unaware": block.start_time_tz_unaware.isoformat(),
                    "end_time_tz_unaware": block.end_time_tz_unaware.isoformat(),
                    "timezone": block.timezone,
                    "reason": block.reason,
                    "rrule": (
                        block.recurrence_rule.to_rrule_string() if block.recurrence_rule else None
                    ),
                }
                update_fields: list[str] = []
                if "start_time" in op:
                    block.start_time_tz_unaware = op["start_time"]
                    update_fields.append("start_time_tz_unaware")
                if "end_time" in op:
                    block.end_time_tz_unaware = op["end_time"]
                    update_fields.append("end_time_tz_unaware")
                if "timezone" in op:
                    block.timezone = op["timezone"]
                    update_fields.append("timezone")
                if "reason" in op:
                    block.reason = op["reason"]
                    update_fields.append("reason")
                if "rrule_string" in op:
                    block.recurrence_rule = self._create_recurrence_rule_if_needed(
                        op["rrule_string"]
                    )
                    update_fields.append("recurrence_rule_fk")
                if update_fields:
                    block.save(update_fields=[*update_fields, "modified"])
                after = {
                    "start_time_tz_unaware": block.start_time_tz_unaware.isoformat(),
                    "end_time_tz_unaware": block.end_time_tz_unaware.isoformat(),
                    "timezone": block.timezone,
                    "reason": block.reason,
                    "rrule": (
                        block.recurrence_rule.to_rrule_string() if block.recurrence_rule else None
                    ),
                }
                self._audit_group_scoped_block_write(
                    AuditAction.UPDATE, acting_principal, block, diff=compute_diff(before, after)
                )
            elif action == "delete":
                block = blocks_by_op_id[op["block_id"]]
                self._audit_group_scoped_block_write(AuditAction.DELETE, acting_principal, block)
                block.delete()

        return list(
            BlockedTime.objects.for_group_slot(group_slot_id)
            .filter_by_organization(organization.id)
            .select_related("recurrence_rule")
            .order_by("pk")
        )

    # ------------------------------------------------------------------
    # Group-scoped quota rules (Phase 3a model / Phase 3c writes of
    # CALENDAR_GROUP_SCOPED_AVAILABILITY)
    # ------------------------------------------------------------------

    def _get_group_scoped_quota_rule(self, rule_id: int) -> CalendarGroupSlotQuotaRule:
        """Fetch a group-scoped ``CalendarGroupSlotQuotaRule`` by id, scoped to
        this org.

        Unlike ``_get_group_scoped_window``/``_get_group_scoped_block``, there
        is no ``unscoped()``/default-exclude split to navigate here -- every
        ``CalendarGroupSlotQuotaRule`` row is group-scoped by construction
        (there is no "base" quota rule), so the default manager already
        returns the right rows.
        """
        org_id = cast(Organization, self.organization).id
        try:
            return (
                CalendarGroupSlotQuotaRule.objects.filter_by_organization(org_id)
                .select_related("group_slot", "calendar")
                .get(id=rule_id)
            )
        except CalendarGroupSlotQuotaRule.DoesNotExist:
            raise CalendarGroupSlotConfigNotFoundError() from None

    def _audit_group_scoped_quota_rule_write(
        self,
        action: str,
        acting_user: "User | SystemUser",
        subject_instance: CalendarGroupSlotQuotaRule,
        diff: dict | None = None,
    ) -> None:
        """Emit an audit record for a group-scoped quota-rule write.

        Mirrors ``_audit_group_scoped_block_write``/``_audit_group_scoped_availability_write``:
        the acting principal is taken explicitly (this write path is reachable
        without a bound ``calendar_service``), and this is a no-op when no
        ``audit_service`` / ``organization`` is bound.
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

    def _find_matching_group_scoped_quota_rule(
        self,
        group_slot_id: int,
        calendar_id: int,
        period: str,
        cap: int,
    ) -> CalendarGroupSlotQuotaRule | None:
        """Find an existing group-scoped quota rule with identical content, if any.

        Backs the batch-upsert write path's idempotent ``create`` (mirrors
        ``_find_matching_group_scoped_block``): replaying the same batch
        (spec UC-5) must land on the identical final state, not attempt to
        insert a duplicate and trip the (calendar, slot, period) unique
        constraint. Matched on (calendar, group slot, period, cap) -- a
        ``create`` naming the SAME period but a DIFFERENT cap is deliberately
        NOT a match, so it falls through to the real insert and surfaces the
        unique-constraint violation as a validation error rather than
        silently keeping the old cap.
        """
        organization = cast(Organization, self.organization)
        return (
            CalendarGroupSlotQuotaRule.objects.for_group_slot(group_slot_id)
            .filter_by_organization(organization.id)
            .filter(calendar_fk_id=calendar_id, period=period, cap=cap)
            .first()
        )

    @transaction.atomic()
    def create_group_scoped_quota_rule(
        self,
        acting_user: User,
        group_slot_id: int,
        calendar_id: int,
        period: str,
        cap: int,
    ) -> CalendarGroupSlotQuotaRule:
        """Create a group-scoped quota rule capping ``calendar_id``'s live
        bookings made through ``group_slot_id`` within one ``period``.

        Permission-gated identically to windows and blocks: ``acting_user``
        must own the calendar or be an org admin (see
        ``CalendarPermissionService.can_manage_group_scoped_calendar_config``).

        Unlike windows and blocks, quota rules are NOT metered (spec: "Windows
        and blocks both consume the limit; quota rules do not") -- only
        ``_check_not_restricted()`` guards this write, never ``check_limit``.
        There is also no orphaned-booking report: a quota rule caps FUTURE
        bookings, it does not narrow or remove time from already-confirmed
        ones, so nothing here can retroactively orphan a booking the way a
        window or block write can.

        :raises CalendarGroupValidationError: ``cap`` is not a positive
            integer, or a rule for ``(calendar_id, group_slot_id, period)``
            already exists (the model's unique constraint) -- surfaced as a
            validation error, never an unhandled ``IntegrityError``.
        """
        self._assert_initialized()
        self._check_not_restricted()
        if cap < 1:
            raise CalendarGroupValidationError("cap must be at least 1.")
        if period not in QuotaPeriod.values:
            raise CalendarGroupValidationError(f"Invalid period: {period!r}.")

        organization = cast(Organization, self.organization)
        membership = self._resolve_group_scoped_membership(group_slot_id, calendar_id)
        self._authorize_group_scoped_write(acting_user, membership.calendar, membership.slot)

        try:
            # Nested atomic() opens a savepoint scoped to just this insert, so
            # catching the IntegrityError below rolls back only the failed
            # insert -- not the whole request transaction (ATOMIC_REQUESTS)
            # nor this method's own outer @transaction.atomic().
            with transaction.atomic():
                rule = CalendarGroupSlotQuotaRule.objects.create(
                    organization=organization,
                    group_slot=membership.slot,
                    calendar=membership.calendar,
                    period=period,
                    cap=cap,
                )
        except IntegrityError as e:
            if self._is_quota_uniqueness_constraint_violation(e):
                raise CalendarGroupValidationError(
                    f"A quota rule for period {period!r} already exists for this calendar and slot."
                ) from e
            raise

        self._audit_group_scoped_quota_rule_write(AuditAction.CREATE, acting_user, rule)
        return rule

    @transaction.atomic()
    def update_group_scoped_quota_rule(
        self,
        acting_user: User,
        rule_id: int,
        period: str | None = None,
        cap: int | None = None,
    ) -> CalendarGroupSlotQuotaRule:
        """Partially update a group-scoped quota rule (only provided fields
        change -- mirrors ``update_group_scoped_blocked_time``).

        :raises CalendarGroupValidationError: ``cap`` is not a positive
            integer, or the update would collide with another rule already
            covering ``(calendar, slot, period)``.
        """
        self._assert_initialized()
        self._check_not_restricted()
        if cap is not None and cap < 1:
            raise CalendarGroupValidationError("cap must be at least 1.")
        if period is not None and period not in QuotaPeriod.values:
            raise CalendarGroupValidationError(f"Invalid period: {period!r}.")

        rule = self._get_group_scoped_quota_rule(rule_id)
        self._authorize_group_scoped_write(acting_user, rule.calendar, rule.group_slot)

        before = {"period": rule.period, "cap": rule.cap}

        update_fields: list[str] = []
        if period is not None:
            rule.period = period
            update_fields.append("period")
        if cap is not None:
            rule.cap = cap
            update_fields.append("cap")

        if update_fields:
            try:
                # See create_group_scoped_quota_rule -- nested atomic() scopes
                # the savepoint to just this save.
                with transaction.atomic():
                    rule.save(update_fields=[*update_fields, "modified"])
            except IntegrityError as e:
                if self._is_quota_uniqueness_constraint_violation(e):
                    raise CalendarGroupValidationError(
                        f"A quota rule for period {rule.period!r} already exists for this "
                        "calendar and slot."
                    ) from e
                raise

        after = {"period": rule.period, "cap": rule.cap}
        self._audit_group_scoped_quota_rule_write(
            AuditAction.UPDATE, acting_user, rule, diff=compute_diff(before, after)
        )
        return rule

    @transaction.atomic()
    def delete_group_scoped_quota_rule(self, acting_user: User, rule_id: int) -> None:
        """Delete a group-scoped quota rule.

        Mirrors ``delete_group_scoped_blocked_time``/``delete_group_scoped_availability_window``:
        no orphaned-booking report is computed (deleting a quota rule only
        WIDENS future bookability, and quota never narrows already-confirmed
        bookings in the first place).
        """
        self._assert_initialized()
        self._check_not_restricted()
        rule = self._get_group_scoped_quota_rule(rule_id)
        self._authorize_group_scoped_write(acting_user, rule.calendar, rule.group_slot)

        self._audit_group_scoped_quota_rule_write(AuditAction.DELETE, acting_user, rule)
        rule.delete()

    @transaction.atomic()
    def batch_upsert_group_scoped_quota_rules(
        self,
        group_slot_id: int,
        operations: Iterable[dict],
        acting_principal: "User | SystemUser",
    ) -> list[CalendarGroupSlotQuotaRule]:
        """Apply an atomic bulk-upsert batch of group-scoped quota-rule
        create/update/delete operations within one group slot's roster
        (CALENDAR_GROUP_SCOPED_AVAILABILITY Phase 3c -- public API).

        Direct mirror of ``batch_upsert_group_scoped_blocked_times``'s
        transaction / idempotent-create / IDOR-cross-check structure -- same
        two deliberate differences from the window batch: quota rules are not
        metered (never ``check_limit``, never locks the billing root), but it
        still calls ``check_not_restricted`` up front so a ``RESTRICTED``
        organization cannot write group-scoped quota rules either.

        * **All-or-nothing.** The whole batch runs inside one transaction.
        * **Idempotent create.** A ``create`` op is an upsert:
          :meth:`_find_matching_group_scoped_quota_rule` looks for an existing
          rule with identical content (calendar, slot, period, cap) first. A
          match is returned unchanged; a genuine content difference on an
          already-used period falls through to the real insert and surfaces
          the unique-constraint violation as :class:`CalendarGroupValidationError`,
          never an unhandled ``IntegrityError``.
        * **update / delete** require ``rule_id`` and act on exactly the row
          it names.

        Authorization is split between the CALLER and this method, exactly
        like the block batch: the caller proves each op's ``calendar_id`` is
        within the token's owner scope; this method cross-checks that an
        update/delete op's resolved rule actually belongs to that op's own
        ``calendar_id`` and rejects the whole batch (not-found-shaped) if
        they don't match.

        :param operations: dicts with ``action`` (create/update/delete) and
            ``calendar_id``; create also takes ``period`` and ``cap``;
            update takes ``rule_id`` plus optional ``period``/``cap``;
            delete takes ``rule_id``.
        :param acting_principal: the ``User`` or public-API ``SystemUser``
            this write is attributed to in the audit trail.
        :raises CalendarGroupSlotConfigNotFoundError: a create op's
            ``calendar_id`` is not a member of ``group_slot_id``'s roster, or
            an update/delete op's ``rule_id`` does not resolve to a
            group-scoped quota rule in this slot, or that rule does not
            belong to the op's own ``calendar_id``.
        :raises CalendarGroupValidationError: a ``cap`` is not a positive
            integer, or a create/update collides with the unique
            (calendar, slot, period) constraint.
        :raises ValueError: an operation's shape is invalid (unknown action,
            missing required field).
        :return: every group-scoped quota rule in ``group_slot_id``'s roster
            (all calendars) after the batch is applied.
        """
        self._assert_initialized()
        organization = cast(Organization, self.organization)
        operations = list(operations)

        valid_actions = {"create", "update", "delete"}
        for op in operations:
            action = op.get("action")
            if action not in valid_actions:
                raise ValueError(f"Invalid operation action: {action}")
            if action == "create" and (
                "calendar_id" not in op or "period" not in op or "cap" not in op
            ):
                raise ValueError("create operation requires calendar_id, period, cap")
            if action in ("update", "delete") and "rule_id" not in op:
                raise ValueError(f"{action} operation requires rule_id")
            if "cap" in op and op["cap"] is not None and op["cap"] < 1:
                raise CalendarGroupValidationError("cap must be at least 1.")
            if (
                "period" in op
                and op["period"] is not None
                and op["period"] not in QuotaPeriod.values
            ):
                raise CalendarGroupValidationError(f"Invalid period: {op['period']!r}.")

        # RESTRICTED must block the batch outright, mirrors the block batch,
        # minus the limit/lock machinery that exists only to protect
        # entitlement counting, which quota rules don't have (spec: unmetered).
        if self.entitlement_service is not None and operations:
            self.entitlement_service.check_not_restricted(organization)

        # Resolve every touched row up front (read-only): a missing/foreign
        # rule_id or a calendar not on this slot's roster fails the whole
        # batch before anything is written.
        rules_by_op_id: dict[int, CalendarGroupSlotQuotaRule] = {}
        for op in operations:
            if op["action"] in ("update", "delete"):
                rule = self._get_group_scoped_quota_rule(op["rule_id"])
                if rule.group_slot_fk_id != group_slot_id:
                    raise CalendarGroupSlotConfigNotFoundError()
                # Cross-check the resolved rule against the op's OWN calendar_id --
                # mirrors the block batch's IDOR guard: a calendar-owner-scoped
                # token could otherwise pair a calendar_id it owns with a
                # rule_id it doesn't, targeting another calendar's quota rule
                # in the same slot.
                if rule.calendar_fk_id != op["calendar_id"]:
                    raise CalendarGroupSlotConfigNotFoundError()
                rules_by_op_id[op["rule_id"]] = rule

        memberships_by_calendar: dict[int, CalendarGroupSlotMembership] = {}
        for op in operations:
            if op["action"] == "create" and op["calendar_id"] not in memberships_by_calendar:
                memberships_by_calendar[op["calendar_id"]] = self._resolve_group_scoped_membership(
                    group_slot_id, op["calendar_id"]
                )

        # Idempotent-create resolution: match each create op against an
        # existing group-scoped quota rule before writing anything.
        matched_existing: dict[int, CalendarGroupSlotQuotaRule] = {}
        for i, op in enumerate(operations):
            if op["action"] != "create":
                continue
            existing = self._find_matching_group_scoped_quota_rule(
                group_slot_id=group_slot_id,
                calendar_id=op["calendar_id"],
                period=op["period"],
                cap=op["cap"],
            )
            if existing is not None:
                matched_existing[i] = existing

        for i, op in enumerate(operations):
            action = op["action"]
            if action == "create":
                if i in matched_existing:
                    continue
                membership = memberships_by_calendar[op["calendar_id"]]
                try:
                    with transaction.atomic():
                        rule = CalendarGroupSlotQuotaRule.objects.create(
                            organization=organization,
                            group_slot=membership.slot,
                            calendar=membership.calendar,
                            period=op["period"],
                            cap=op["cap"],
                        )
                except IntegrityError as e:
                    if self._is_quota_uniqueness_constraint_violation(e):
                        raise CalendarGroupValidationError(
                            f"A quota rule for period {op['period']!r} already exists for "
                            f"calendar {op['calendar_id']} in this slot."
                        ) from e
                    raise
                self._audit_group_scoped_quota_rule_write(
                    AuditAction.CREATE, acting_principal, rule
                )
            elif action == "update":
                rule = rules_by_op_id[op["rule_id"]]
                before = {"period": rule.period, "cap": rule.cap}
                update_fields: list[str] = []
                if "period" in op:
                    rule.period = op["period"]
                    update_fields.append("period")
                if "cap" in op:
                    rule.cap = op["cap"]
                    update_fields.append("cap")
                if update_fields:
                    try:
                        with transaction.atomic():
                            rule.save(update_fields=[*update_fields, "modified"])
                    except IntegrityError as e:
                        if self._is_quota_uniqueness_constraint_violation(e):
                            raise CalendarGroupValidationError(
                                f"A quota rule for period {rule.period!r} already exists for "
                                "this calendar and slot."
                            ) from e
                        raise
                after = {"period": rule.period, "cap": rule.cap}
                self._audit_group_scoped_quota_rule_write(
                    AuditAction.UPDATE, acting_principal, rule, diff=compute_diff(before, after)
                )
            elif action == "delete":
                rule = rules_by_op_id[op["rule_id"]]
                self._audit_group_scoped_quota_rule_write(
                    AuditAction.DELETE, acting_principal, rule
                )
                rule.delete()

        return list(
            CalendarGroupSlotQuotaRule.objects.for_group_slot(group_slot_id)
            .filter_by_organization(organization.id)
            .order_by("pk")
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

    def _slot_pools_with_group_scoped_flags(
        self, slots: Iterable[CalendarGroupSlot]
    ) -> tuple[dict[int, set[int]], dict[int, set[int]], dict[int, set[int]], dict[int, set[int]]]:
        """Build the (slot_id -> calendar_id pool) map, folding in three
        per-row ``EXISTS`` subqueries flagging which pool calendars have ANY
        group-scoped availability window, ANY group-scoped blocked time, and
        ANY group-scoped quota rule configured for that slot (regardless of
        whether any of them overlaps a caller's search window).

        This is the Phase 1b (windows) / Phase 2a (blocks) / Phase 3b (quota)
        self-gating early-out mechanism (``CALENDAR_GROUP_SCOPED_AVAILABILITY``):
        all three ``EXISTS`` clauses are folded into the SAME per-slot
        membership query every caller of this method already issued before
        Phase 1b, so an unconfigured group costs exactly as many round trips
        as it did before any of the three phases -- zero added queries. Only
        when the returned "window configured", "block configured", or "quota
        configured" map is non-empty for a slot does the caller go on to
        fetch the actual group-scoped spans / rules (a fixed, non-per-candidate
        number of additional queries -- see
        ``slot_engine.fetch_group_scoped_available_spans`` /
        ``slot_engine.fetch_group_scoped_blocking_spans`` /
        ``slot_engine.fetch_group_scoped_quota_rules``).

        Returns ``(slot_pool_by_id, group_scoped_window_calendar_ids_by_slot,
        group_scoped_block_calendar_ids_by_slot,
        group_scoped_quota_calendar_ids_by_slot)``. The second, third, and
        fourth mappings each omit a slot entirely when nothing in its pool is
        configured, so ``bool(...)`` on any alone tells a caller whether ANY
        group-scoped window / block / quota rule exists anywhere in the group.
        """
        org_id = cast(Organization, self.organization).id
        slot_pool_by_id: dict[int, set[int]] = {}
        group_scoped_window_calendar_ids_by_slot: dict[int, set[int]] = {}
        group_scoped_block_calendar_ids_by_slot: dict[int, set[int]] = {}
        group_scoped_quota_calendar_ids_by_slot: dict[int, set[int]] = {}
        for s in slots:
            rows = (
                CalendarGroupSlotMembership.objects.filter_by_organization(org_id)
                .filter(slot_fk=s)
                .annotate(
                    has_group_scoped_window=Exists(
                        AvailableTime.objects.unscoped()
                        .filter_by_organization(org_id)
                        .filter(calendar_fk_id=OuterRef("calendar_fk_id"), group_slot_fk_id=s.id)
                    ),
                    has_group_scoped_block=Exists(
                        BlockedTime.objects.unscoped()
                        .filter_by_organization(org_id)
                        .filter(calendar_fk_id=OuterRef("calendar_fk_id"), group_slot_fk_id=s.id)
                    ),
                    has_group_scoped_quota=Exists(
                        CalendarGroupSlotQuotaRule.objects.filter_by_organization(org_id).filter(
                            calendar_fk_id=OuterRef("calendar_fk_id"), group_slot_fk_id=s.id
                        )
                    ),
                )
                .values_list(
                    "calendar_fk_id",
                    "has_group_scoped_window",
                    "has_group_scoped_block",
                    "has_group_scoped_quota",
                )
            )
            pool: set[int] = set()
            window_configured: set[int] = set()
            block_configured: set[int] = set()
            quota_configured: set[int] = set()
            for cid, has_window, has_block, has_quota in rows:
                pool.add(cid)
                if has_window:
                    window_configured.add(cid)
                if has_block:
                    block_configured.add(cid)
                if has_quota:
                    quota_configured.add(cid)
            slot_pool_by_id[s.id] = pool
            if window_configured:
                group_scoped_window_calendar_ids_by_slot[s.id] = window_configured
            if block_configured:
                group_scoped_block_calendar_ids_by_slot[s.id] = block_configured
            if quota_configured:
                group_scoped_quota_calendar_ids_by_slot[s.id] = quota_configured
        return (
            slot_pool_by_id,
            group_scoped_window_calendar_ids_by_slot,
            group_scoped_block_calendar_ids_by_slot,
            group_scoped_quota_calendar_ids_by_slot,
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

        Group-scoped availability windows (``CALENDAR_GROUP_SCOPED_AVAILABILITY``
        Phase 1b) are intersected in AFTER base availability: a calendar with no
        group-scoped window configured for a slot is unaffected (fall-through
        default, zero added queries -- see
        ``_slot_pools_with_group_scoped_flags``); a calendar WITH one is listed
        as available for a range only when that range is fully covered by at
        least one of its group-scoped windows -- narrowing only, never widening.

        Group-scoped blocked time (Phase 2a) is applied AFTER base availability
        and BEFORE the window check, per the spec resolution order: a calendar
        with a configured block that OVERLAPS the range is excluded regardless
        of what any window says ("blocks beat everything"). Also zero added
        queries when unconfigured.

        Group-scoped quota rules (Phase 3b) are checked LAST, after base
        availability, block, and window all pass: a calendar with a
        configured quota rule whose cap is already met for the period the
        range's start falls into is excluded, regardless of what any window
        says. Also zero added queries when unconfigured -- see
        ``slot_engine.fetch_group_scoped_quota_period_counts`` for the
        query-count discipline (one counting query per ``(slot, period)``
        combination actually configured, covering every range passed to this
        call, never one per range).
        """
        self._assert_initialized()
        group = self._get_group_by_id(group_id)
        ranges = list(ranges)

        slots = list(group.slots.all())
        (
            slot_pool_by_id,
            group_scoped_window_calendar_ids_by_slot,
            group_scoped_block_calendar_ids_by_slot,
            group_scoped_quota_calendar_ids_by_slot,
        ) = self._slot_pools_with_group_scoped_flags(slots)

        # Self-gating early-out: only fetch expanded group-scoped spans when at
        # least one calendar anywhere in the group actually has one configured.
        # Compute union range once for the window, block, and quota fetches.
        union_start = union_end = None
        if ranges:
            union_start = min(start for start, _ in ranges)
            union_end = max(end for _, end in ranges)

        group_scoped_spans_by_slot: slot_engine.GroupScopedSpansBySlot = {}
        if (
            group_scoped_window_calendar_ids_by_slot
            and union_start is not None
            and union_end is not None
        ):
            configured_slot_ids = list(group_scoped_window_calendar_ids_by_slot.keys())
            configured_calendar_ids: set[int] = set()
            for ids in group_scoped_window_calendar_ids_by_slot.values():
                configured_calendar_ids.update(ids)
            group_scoped_spans_by_slot = slot_engine.fetch_group_scoped_available_spans(
                self.organization.id,
                configured_slot_ids,
                configured_calendar_ids,
                union_start,
                union_end,
            )

        group_scoped_block_spans_by_slot: slot_engine.GroupScopedSpansBySlot = {}
        if (
            group_scoped_block_calendar_ids_by_slot
            and union_start is not None
            and union_end is not None
        ):
            configured_block_slot_ids = list(group_scoped_block_calendar_ids_by_slot.keys())
            configured_block_calendar_ids: set[int] = set()
            for ids in group_scoped_block_calendar_ids_by_slot.values():
                configured_block_calendar_ids.update(ids)
            group_scoped_block_spans_by_slot = slot_engine.fetch_group_scoped_blocking_spans(
                self.organization.id,
                configured_block_slot_ids,
                configured_block_calendar_ids,
                union_start,
                union_end,
            )

        group_scoped_quota_rules_by_slot: slot_engine.GroupScopedQuotaRulesBySlot = {}
        group_scoped_quota_counts_by_slot: slot_engine.GroupScopedQuotaCountsBySlot = {}
        org = cast(Organization, self.organization)
        week_start = org.week_start
        if (
            group_scoped_quota_calendar_ids_by_slot
            and union_start is not None
            and union_end is not None
        ):
            configured_quota_slot_ids = list(group_scoped_quota_calendar_ids_by_slot.keys())
            configured_quota_calendar_ids: set[int] = set()
            for ids in group_scoped_quota_calendar_ids_by_slot.values():
                configured_quota_calendar_ids.update(ids)
            quota_rules = slot_engine.fetch_group_scoped_quota_rules(
                org.id, configured_quota_slot_ids, configured_quota_calendar_ids
            )
            group_scoped_quota_rules_by_slot = slot_engine.group_quota_rules_by_slot(quota_rules)
            # Widen to the full period boundaries touched by any range's
            # start -- the literal `[union_start, union_end)` union of the
            # requested ranges can start AFTER a period's own start (e.g. a
            # Wednesday range inside a Monday-start week), which would
            # silently undercount earlier live bookings in that same period.
            # See `slot_engine.quota_covering_range`'s docstring.
            covering_range = slot_engine.quota_covering_range(
                (start for start, _ in ranges),
                {rule.period for rule in quota_rules},
                week_start,
            )
            if covering_range is not None:
                group_scoped_quota_counts_by_slot = (
                    slot_engine.fetch_group_scoped_quota_period_counts(
                        org.id, quota_rules, week_start, *covering_range
                    )
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
                window_configured_ids = group_scoped_window_calendar_ids_by_slot.get(s.id)
                block_configured_ids = group_scoped_block_calendar_ids_by_slot.get(s.id)
                quota_configured_ids = group_scoped_quota_calendar_ids_by_slot.get(s.id)
                if (
                    not window_configured_ids
                    and not block_configured_ids
                    and not quota_configured_ids
                ):
                    final_available = base_available
                else:
                    window_spans_for_slot = group_scoped_spans_by_slot.get(s.id, {})
                    block_spans_for_slot = group_scoped_block_spans_by_slot.get(s.id, {})
                    quota_rules_for_slot = group_scoped_quota_rules_by_slot.get(s.id, {})
                    quota_counts_for_slot = group_scoped_quota_counts_by_slot.get(s.id, {})
                    final_available = set()
                    for cid in base_available:
                        # Blocks beat everything -- checked first, before windows.
                        if block_configured_ids and cid in block_configured_ids:
                            blocked = any(
                                slot_engine.intervals_overlap(span, (start, end))
                                for span in block_spans_for_slot.get(cid, ())
                            )
                            if blocked:
                                continue
                        if window_configured_ids and cid in window_configured_ids:
                            if not slot_engine.window_fully_covered_by_spans(
                                window_spans_for_slot.get(cid, ()), start, end
                            ):
                                continue
                        # Quota is checked LAST, after block and window both pass.
                        if quota_configured_ids and cid in quota_configured_ids:
                            over_quota = False
                            for rule in quota_rules_for_slot.get(cid, ()):
                                period_start = slot_engine.quota_period_start_utc(
                                    start, rule.period, week_start
                                )
                                count = quota_counts_for_slot.get((cid, rule.period), {}).get(
                                    period_start, 0
                                )
                                if count >= rule.cap:
                                    over_quota = True
                                    break
                            if over_quota:
                                continue
                        final_available.add(cid)
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
        Group-scoped blocks (Phase 2a) and quota rules (Phase 3b) are checked
        the same way.

        Quota self-exclusion: the count used to validate the NEW period is
        the live count as of THIS event's still-unmoved ``CalendarEvent`` row
        (the reschedule write happens after this check), which would
        otherwise still include this event's own booking if it already falls
        in the SAME target period (e.g. moving a booking from 9am to 2pm on
        the same day, under a daily cap already at its limit). To avoid
        rejecting such a same-period, no-net-change reschedule,
        ``_assert_calendars_within_group_scoped_windows`` is passed this
        event's CURRENT (pre-move) start time and subtracts 1 from a rule's
        looked-up count whenever that old period matches the candidate's
        period -- see that method's docstring for the full rationale.
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
        self._assert_calendars_within_group_scoped_windows(
            selection_pairs,
            start_time,
            end_time,
            rescheduling_event_old_start=event.start_time,
        )

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
        rescheduling_event_old_start: datetime.datetime | None = None,
    ) -> None:
        """Reject any ``(slot_id, calendar_id)`` pair whose calendar violates a
        group-scoped rule configured for that slot at ``[start, end)`` (spec
        Acceptance 4, UC-4: a caller cannot book, by naming the calendar
        directly, a time discovery would never have offered).

        Checks BLOCKS, then WINDOWS, then QUOTA -- the same resolution order
        ``slot_engine.calendar_free_for_window`` applies in discovery ("blocks
        beat everything", quota last): a pair with a configured group-scoped
        block that OVERLAPS ``[start, end)`` is rejected with
        ``GroupScopedRuleType.INSIDE_BLOCK`` before the window check even
        runs, regardless of what any window or quota rule says. A pair with
        no block hit is then rejected with ``GroupScopedRuleType.OUTSIDE_WINDOW``
        if it has a configured window but ``[start, end)`` is not fully
        covered by any of them. Only once BOTH pass does a pair with a
        configured quota rule get rejected with
        ``GroupScopedRuleType.QUOTA_CONSUMED`` when ANY of its rules (e.g. a
        daily AND a weekly cap) has no headroom left for the period
        ``start`` falls into -- ALL of a calendar's quota rules must have
        headroom, mirroring discovery's "every rule must pass".

        A calendar with NEITHER a group-scoped block, window, NOR quota rule
        for a given ``(calendar, slot)`` pair falls through untouched --
        narrowing/exclusion only ever applies to what was actually
        configured. Self-gating -- the block, window, and quota existence
        checks are each the only extra query when nothing of that kind is
        configured for any of the named pairs; each (fixed-cost) expanded
        fetch only runs when at least one pair IS configured for that kind.
        Called from both ``create_grouped_event`` (after the
        base-availability check) and ``reschedule_grouped_event`` (spec:
        every enforcement surface agrees).

        ``rescheduling_event_old_start``: only passed by
        ``reschedule_grouped_event``, as the event-being-moved's CURRENT
        (pre-move) start time. The live count fetched for quota purposes
        still includes that event's own still-unmoved booking row. When its
        old period matches the candidate's period, this method subtracts 1
        from the looked-up count before comparing to the cap so a same-period
        (no net quota change) reschedule is not rejected purely because it
        sees its own row. A fresh ``create_grouped_event`` call never passes
        this (there is no prior row to exclude), and discovery
        (``calendar_free_for_window`` / ``check_group_availability``) never
        calls this method at all -- both are therefore unaffected.
        """
        pairs = list(slot_calendar_pairs)
        if not pairs:
            return

        org_id = cast(Organization, self.organization).id
        slot_ids = {slot_id for slot_id, _ in pairs}
        calendar_ids = {cid for _, cid in pairs}

        # Blocks beat everything -- checked first, before windows and quota.
        configured_block_pairs = set(
            BlockedTime.objects.unscoped()
            .filter_by_organization(org_id)
            .filter(group_slot_fk_id__in=slot_ids, calendar_fk_id__in=calendar_ids)
            .values_list("group_slot_fk_id", "calendar_fk_id")
            .distinct()
        )
        if configured_block_pairs:
            configured_block_slot_ids = {slot_id for slot_id, _ in configured_block_pairs}
            configured_block_calendar_ids = {cid for _, cid in configured_block_pairs}
            block_spans_by_slot = slot_engine.fetch_group_scoped_blocking_spans(
                org_id, configured_block_slot_ids, configured_block_calendar_ids, start, end
            )
            for slot_id, calendar_id in pairs:
                if (slot_id, calendar_id) not in configured_block_pairs:
                    continue
                spans = block_spans_by_slot.get(slot_id, {}).get(calendar_id, ())
                if any(slot_engine.intervals_overlap(span, (start, end)) for span in spans):
                    raise CalendarGroupScopedRuleViolationError(
                        calendar_id=calendar_id, rule_type=GroupScopedRuleType.INSIDE_BLOCK
                    )

        configured_pairs = set(
            AvailableTime.objects.unscoped()
            .filter_by_organization(org_id)
            .filter(group_slot_fk_id__in=slot_ids, calendar_fk_id__in=calendar_ids)
            .values_list("group_slot_fk_id", "calendar_fk_id")
            .distinct()
        )
        if configured_pairs:
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

        # Quota is checked LAST, after block and window both pass.
        configured_quota_rules = slot_engine.fetch_group_scoped_quota_rules(
            org_id, slot_ids, calendar_ids
        )
        if not configured_quota_rules:
            return

        quota_rules_by_slot = slot_engine.group_quota_rules_by_slot(configured_quota_rules)
        week_start = cast(Organization, self.organization).week_start
        # Widen to the full period boundaries the candidate's own start falls
        # into -- `[start, end)` is typically much narrower than a day/week/
        # month bucket, which would otherwise silently undercount an earlier
        # live booking in that same period. See
        # `slot_engine.quota_covering_range`'s docstring.
        covering_range = slot_engine.quota_covering_range(
            (start,), {rule.period for rule in configured_quota_rules}, week_start
        )
        quota_counts_by_slot: slot_engine.GroupScopedQuotaCountsBySlot = {}
        if covering_range is not None:
            quota_counts_by_slot = slot_engine.fetch_group_scoped_quota_period_counts(
                org_id, configured_quota_rules, week_start, *covering_range
            )

        for slot_id, calendar_id in pairs:
            rules = quota_rules_by_slot.get(slot_id, {}).get(calendar_id, ())
            if not rules:
                continue
            counts_for_slot = quota_counts_by_slot.get(slot_id, {})
            for rule in rules:
                period_start = slot_engine.quota_period_start_utc(start, rule.period, week_start)
                count = counts_for_slot.get((calendar_id, rule.period), {}).get(period_start, 0)
                # Self-exclusion for reschedules: the fetched count above
                # already includes the event-being-moved's OWN still-present
                # row. If its old (pre-move) period is the SAME period the
                # candidate falls into, subtract 1 so we're comparing "does
                # this booking exceed the cap for OTHERS" rather than
                # double-counting the event against itself. If the old period
                # differs (a reschedule across a period boundary), the event
                # is NOT part of this period's count, so no adjustment is
                # made and a full period still correctly rejects.
                if rescheduling_event_old_start is not None:
                    old_period_start = slot_engine.quota_period_start_utc(
                        rescheduling_event_old_start, rule.period, week_start
                    )
                    if old_period_start == period_start:
                        count -= 1
                if count >= rule.cap:
                    raise CalendarGroupScopedRuleViolationError(
                        calendar_id=calendar_id, rule_type=GroupScopedRuleType.QUOTA_CONSUMED
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

        Group-scoped blocked time (Phase 2a) is applied AFTER base
        availability and BEFORE the window check (spec resolution order,
        "blocks beat everything"): a calendar with a configured block that
        OVERLAPS the candidate window is excluded from its slot's count
        regardless of what any window says. Also zero added queries when
        unconfigured.

        Group-scoped quota rules (``CALENDAR_GROUP_SCOPED_AVAILABILITY``
        Phase 3b) are checked LAST, after base availability, block, and
        window all pass: a calendar with a configured quota rule at or over
        its cap for the period a candidate window's start falls into is
        excluded from its slot's count, regardless of what any window says.
        The counting call (see ``slot_engine.fetch_group_scoped_quota_period_counts``)
        is issued ONCE per ``(slot, period)`` combination actually
        configured, covering the WHOLE search window -- never once per
        candidate. Also zero added queries when unconfigured.
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

        (
            slot_pool_by_id,
            group_scoped_calendar_ids_by_slot,
            group_scoped_block_calendar_ids_by_slot,
            group_scoped_quota_calendar_ids_by_slot,
        ) = self._slot_pools_with_group_scoped_flags(slots)
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

        # ------------------------------------------------------------------
        # Group-scoped blocked time (CALENDAR_GROUP_SCOPED_AVAILABILITY
        # Phase 2a) -- self-gating early-out, same shape as windows above.
        # ------------------------------------------------------------------
        group_scoped_block_spans_by_slot: slot_engine.GroupScopedSpansBySlot = {}
        if group_scoped_block_calendar_ids_by_slot:
            configured_block_slot_ids = list(group_scoped_block_calendar_ids_by_slot.keys())
            configured_block_calendar_ids: set[int] = set()
            for ids in group_scoped_block_calendar_ids_by_slot.values():
                configured_block_calendar_ids.update(ids)
            group_scoped_block_spans_by_slot = slot_engine.fetch_group_scoped_blocking_spans(
                self.organization.id,
                configured_block_slot_ids,
                configured_block_calendar_ids,
                search_window_start,
                search_window_end,
            )

        # ------------------------------------------------------------------
        # Group-scoped quota rules (CALENDAR_GROUP_SCOPED_AVAILABILITY Phase
        # 3b) -- self-gating early-out, same shape as windows/blocks above.
        # ------------------------------------------------------------------
        # `group_scoped_quota_calendar_ids_by_slot` was already computed above
        # by folding a THIRD EXISTS() subquery into the SAME per-slot
        # membership query -- zero added round trips. Only when at least one
        # calendar anywhere in the group actually has a quota rule configured
        # do we pay for the fixed, non-per-candidate counting fetch below --
        # ONE query per (slot, period) combination actually configured,
        # covering the WHOLE search window in one shot (see
        # `slot_engine.fetch_group_scoped_quota_period_counts`). The number of
        # counting queries is a function of the roster/config, never of how
        # many candidate windows the loop below will check the result
        # against.
        org = cast(Organization, self.organization)
        week_start = org.week_start
        group_scoped_quota_rules_by_slot: slot_engine.GroupScopedQuotaRulesBySlot = {}
        group_scoped_quota_counts_by_slot: slot_engine.GroupScopedQuotaCountsBySlot = {}
        if group_scoped_quota_calendar_ids_by_slot:
            configured_quota_slot_ids = list(group_scoped_quota_calendar_ids_by_slot.keys())
            configured_quota_calendar_ids: set[int] = set()
            for ids in group_scoped_quota_calendar_ids_by_slot.values():
                configured_quota_calendar_ids.update(ids)
            quota_rules = slot_engine.fetch_group_scoped_quota_rules(
                org.id, configured_quota_slot_ids, configured_quota_calendar_ids
            )
            group_scoped_quota_rules_by_slot = slot_engine.group_quota_rules_by_slot(quota_rules)
            # Widen to the full period boundaries touched by the search
            # window's own edges -- `search_window_start` need not itself sit
            # on a period boundary (e.g. a Wednesday-to-Friday search window
            # under a Monday-start weekly rule), which would otherwise
            # silently undercount live bookings made earlier in that same
            # period. See `slot_engine.quota_covering_range`'s docstring.
            covering_range = slot_engine.quota_covering_range(
                (search_window_start, search_window_end),
                {rule.period for rule in quota_rules},
                week_start,
            )
            if covering_range is not None:
                group_scoped_quota_counts_by_slot = (
                    slot_engine.fetch_group_scoped_quota_period_counts(
                        org.id, quota_rules, week_start, *covering_range
                    )
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
                        group_scoped_block_calendar_ids_by_slot.get(slot_id),
                        group_scoped_block_spans_by_slot.get(slot_id),
                        group_scoped_quota_calendar_ids_by_slot.get(slot_id),
                        group_scoped_quota_rules_by_slot.get(slot_id),
                        group_scoped_quota_counts_by_slot.get(slot_id),
                        week_start,
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
