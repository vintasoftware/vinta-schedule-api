"""BookingPolicyService — resolver and CRUD for BookingPolicy.

Resolves the effective booking policy for a single calendar, a bundle calendar,
or a calendar group via the deterministic precedence chain defined in the plan:

- **Single calendar**: calendar policy → owning-membership policy → org-default
  policy → unconstrained.  Owning membership is resolved through
  ``CalendarOwnership``: lone owner, else ``is_default=True`` when there are
  multiple, else skip the membership layer.

- **Bundle calendar**: explicit bundle/calendar policy (the bundle *calendar*
  itself is looked up by calendar FK) → most-restrictive combination across all
  ``bundle_children`` via ``most_restrictive(EffectivePolicy.from_model(p) ...)``
  → unconstrained.

- **Calendar group**: explicit group policy (looked up by calendar_group FK) →
  most-restrictive combination across all calendars that belong to any slot in
  the group → unconstrained.

All write paths (create / update / delete) emit ``AuditService`` records and
enforce the uniqueness contract (one policy per target per org).
"""

from typing import TYPE_CHECKING, Annotated

from dependency_injector.wiring import Provide, inject

from audit.constants import AuditAction
from audit.diff import compute_diff
from calendar_integration.exceptions import (
    CalendarServiceOrganizationNotSetError,
)
from calendar_integration.models import (
    BookingPolicy,
    Calendar,
    CalendarGroup,
    CalendarGroupSlotMembership,
    CalendarOwnership,
    ChildrenCalendarRelationship,
)
from calendar_integration.services.dataclasses import EffectivePolicy
from organizations.models import Organization


if TYPE_CHECKING:
    from audit.services import AuditService


class DuplicateBookingPolicyError(Exception):
    """Raised when a second BookingPolicy is created for the same target/org.

    Callers (REST serializers, GraphQL mutations) should map this to a 400 /
    validation error with the message surfaced to the client.
    """

    pass


class BookingPolicyService:
    """Service for booking-policy resolution and CRUD.

    Must be initialized with ``initialize(organization)`` before use.  All query
    paths are fully organization-scoped through the ``BookingPolicyManager``
    helpers introduced in Phase 1 — no raw or unscoped queries are made.
    """

    organization: Organization | None

    @inject
    def __init__(
        self,
        audit_service: Annotated["AuditService | None", Provide["audit_service"]] = None,
    ) -> None:
        self.organization = None
        self.audit_service = audit_service
        # Optional actor context for audit records — set by the caller via
        # ``set_actor`` when the acting principal is known (e.g. a REST view or
        # GraphQL mutation that has an authenticated user/system-user).
        self._actor: object | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self, organization: Organization) -> None:
        """Bind this service instance to a tenant organization."""
        self.organization = organization

    def set_actor(self, actor: object) -> None:
        """Capture the acting principal for audit records.

        Pass the resolved ``ActorSnapshot`` from the calling layer (REST view or
        GraphQL resolver) so the audit trail records the right actor.  When not
        called, ``_actor`` stays ``None`` and the audit helper falls back to the
        system actor.
        """
        self._actor = actor

    def _assert_initialized(self) -> None:
        if self.organization is None:
            raise CalendarServiceOrganizationNotSetError(
                "BookingPolicyService requires an organization. Call initialize()."
            )

    # ------------------------------------------------------------------
    # Audit helper
    # ------------------------------------------------------------------

    def _audit_write(
        self,
        action: str,
        policy: BookingPolicy,
        diff: dict | None = None,
    ) -> None:
        """Emit an audit record for a booking-policy business write.

        No-op when ``audit_service`` or ``organization`` is not bound — so
        instrumentation never breaks a write path (mirrors the pattern used by
        ``CalendarService._audit_calendar_write`` and
        ``CalendarGroupService._audit_group_write``).
        """
        if self.audit_service is None or self.organization is None:
            return
        from audit.services import AuditService

        actor = self._actor
        if actor is None:
            actor = AuditService.system_actor()
        self.audit_service.record(
            organization_id=self.organization.id,
            action=action,
            actor=actor,  # type: ignore[arg-type]
            subject=self.audit_service.subject_from_instance(policy),
            diff=diff,
        )

    # ------------------------------------------------------------------
    # Internal query helpers (all org-scoped via manager)
    # ------------------------------------------------------------------

    def _policy_for_calendar(self, calendar_id: int) -> BookingPolicy | None:
        """Return the calendar-level policy for ``calendar_id``, or ``None``."""
        return BookingPolicy.objects.for_target(
            self.organization.id,  # type: ignore[union-attr]
            calendar_id=calendar_id,
        )

    def _policy_for_membership(self, membership_user_id: int) -> BookingPolicy | None:
        """Return the membership-level policy for ``membership_user_id``, or ``None``."""
        return BookingPolicy.objects.for_target(
            self.organization.id,  # type: ignore[union-attr]
            membership_user_id=membership_user_id,
        )

    def _policy_for_group(self, calendar_group_id: int) -> BookingPolicy | None:
        """Return the group-level policy for ``calendar_group_id``, or ``None``."""
        return BookingPolicy.objects.for_target(
            self.organization.id,  # type: ignore[union-attr]
            calendar_group_id=calendar_group_id,
        )

    def _org_default_policy(self) -> BookingPolicy | None:
        """Return the organization-default policy, or ``None``."""
        return BookingPolicy.objects.org_default(self.organization.id)  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Owning-membership resolution
    # ------------------------------------------------------------------

    def _resolve_owning_membership_user_id(self, calendar: Calendar) -> int | None:
        """Resolve the single owning-membership user id for ``calendar``.

        Rules (from the plan's Guiding Decisions):
        1. Exactly one ``CalendarOwnership`` with non-NULL ``membership_user_id``
           → return that user id.
        2. Multiple ownerships → return the ``is_default=True`` one's user id.
        3. Zero non-NULL ownerships → return ``None`` (skip the membership layer;
           resource / shared calendars).

        Orphan ownerships (``membership_user_id IS NULL``) are excluded from the
        count and from the resolution (they carry no membership FK and therefore
        no membership policy can be attached to them).
        """
        # Filter to ownerships that belong to this org and have a real membership.
        # We read through the CalendarOwnership manager which is an OrganizationModel
        # so we must use filter_by_organization.
        ownerships = list(
            CalendarOwnership.objects.filter_by_organization(self.organization.id)  # type: ignore[union-attr]
            .filter(calendar_fk_id=calendar.id)
            .exclude(membership_user_id__isnull=True)
            .values_list("membership_user_id", "is_default")
        )

        if not ownerships:
            return None
        if len(ownerships) == 1:
            membership_user_id, _ = ownerships[0]
            return membership_user_id

        # Multiple ownerships — pick the is_default=True one.
        for membership_user_id, is_default in ownerships:
            if is_default:
                return membership_user_id

        # Multiple ownerships with no is_default=True → skip the membership layer.
        return None

    # ------------------------------------------------------------------
    # Resolution API
    # ------------------------------------------------------------------

    def resolve_for_calendar(self, calendar: Calendar) -> EffectivePolicy:
        """Resolve the effective booking policy for a single (non-bundle) calendar.

        Precedence:
        1. Calendar-level policy.
        2. Owning-membership policy (lone owner, or ``is_default`` owner when
           several; otherwise skip).
        3. Org-default policy.
        4. ``EffectivePolicy.unconstrained()``.
        """
        self._assert_initialized()

        # 1. Calendar-level policy.
        cal_policy = self._policy_for_calendar(calendar.id)
        if cal_policy is not None:
            return EffectivePolicy.from_model(cal_policy)

        # 2. Owning-membership policy.
        owning_user_id = self._resolve_owning_membership_user_id(calendar)
        if owning_user_id is not None:
            mem_policy = self._policy_for_membership(owning_user_id)
            if mem_policy is not None:
                return EffectivePolicy.from_model(mem_policy)

        # 3. Org-default policy.
        default_policy = self._org_default_policy()
        if default_policy is not None:
            return EffectivePolicy.from_model(default_policy)

        # 4. No policy anywhere → unconstrained.
        return EffectivePolicy.unconstrained()

    def resolve_for_bundle(self, bundle_calendar: Calendar) -> EffectivePolicy:
        """Resolve the effective booking policy for a bundle calendar.

        Precedence:
        1. Explicit policy attached directly to the bundle calendar (calendar FK).
        2. ``most_restrictive`` combination across all ``bundle_children``
           calendars, where each child's policy is resolved via
           ``resolve_for_calendar``.
        3. ``EffectivePolicy.unconstrained()``.
        """
        self._assert_initialized()

        # 1. Explicit bundle-calendar policy.
        bundle_policy = self._policy_for_calendar(bundle_calendar.id)
        if bundle_policy is not None:
            return EffectivePolicy.from_model(bundle_policy)

        # 2. Most-restrictive across all child calendars.
        child_calendar_ids = list(
            ChildrenCalendarRelationship.objects.filter_by_organization(self.organization.id)  # type: ignore[union-attr]
            .filter(bundle_calendar_fk_id=bundle_calendar.pk)
            .values_list("child_calendar_fk_id", flat=True)
        )
        if child_calendar_ids:
            child_calendars = list(
                Calendar.objects.filter_by_organization(self.organization.id).filter(  # type: ignore[union-attr]
                    id__in=child_calendar_ids
                )
            )
            child_policies = [self.resolve_for_calendar(cal) for cal in child_calendars]
            combined = EffectivePolicy.most_restrictive(child_policies)
            if combined != EffectivePolicy.unconstrained():
                return combined

        # 3. Unconstrained.
        return EffectivePolicy.unconstrained()

    def resolve_for_group(self, group: CalendarGroup) -> EffectivePolicy:
        """Resolve the effective booking policy for a calendar group.

        Precedence:
        1. Explicit policy attached to the group (calendar_group FK).
        2. ``most_restrictive`` combination across all calendars that belong to
           any slot in the group, each resolved via ``resolve_for_calendar``.
        3. ``EffectivePolicy.unconstrained()``.
        """
        self._assert_initialized()

        # 1. Explicit group policy.
        group_policy = self._policy_for_group(group.id)
        if group_policy is not None:
            return EffectivePolicy.from_model(group_policy)

        # 2. Most-restrictive across all participant calendars.
        # Collect all distinct calendar IDs across every slot of the group.
        # Use slot_fk__group_fk_id to traverse the concrete FK columns.
        participant_calendar_ids = list(
            CalendarGroupSlotMembership.objects.filter_by_organization(self.organization.id)  # type: ignore[union-attr]
            .filter(slot_fk__group_fk_id=group.id)
            .values_list("calendar_fk_id", flat=True)
            .distinct()
        )
        if participant_calendar_ids:
            participant_calendars = list(
                Calendar.objects.filter_by_organization(self.organization.id).filter(  # type: ignore[union-attr]
                    id__in=participant_calendar_ids
                )
            )
            participant_policies = [self.resolve_for_calendar(cal) for cal in participant_calendars]
            combined = EffectivePolicy.most_restrictive(participant_policies)
            if combined != EffectivePolicy.unconstrained():
                return combined

        # 3. Unconstrained.
        return EffectivePolicy.unconstrained()

    # ------------------------------------------------------------------
    # Write API (create / update / delete)
    # ------------------------------------------------------------------

    def create_booking_policy(
        self,
        *,
        calendar: Calendar | None = None,
        membership_user_id: int | None = None,
        calendar_group: CalendarGroup | None = None,
        is_organization_default: bool = False,
        lead_time_seconds: int = 0,
        max_horizon_seconds: int = 0,
        buffer_before_seconds: int = 0,
        buffer_after_seconds: int = 0,
    ) -> BookingPolicy:
        """Create a new BookingPolicy for the bound organization.

        Exactly one target must be specified.  Raises ``DuplicateBookingPolicyError``
        when a policy already exists for the same target (mirroring the DB's
        partial-unique-index constraint, but caught earlier with a clear message).
        Emits an ``AuditService`` CREATE record on success.
        """
        self._assert_initialized()

        targets_set = [
            calendar is not None,
            membership_user_id is not None,
            calendar_group is not None,
            is_organization_default,
        ]
        if sum(targets_set) != 1:
            raise ValueError(
                "create_booking_policy requires exactly one target: calendar, "
                "membership_user_id, calendar_group, or is_organization_default."
            )

        org_id = self.organization.id  # type: ignore[union-attr]
        org = self.organization  # type: ignore[assignment]

        # Check uniqueness before hitting the DB constraint to provide a clear error.
        if calendar is not None and self._policy_for_calendar(calendar.id) is not None:
            raise DuplicateBookingPolicyError(
                f"A BookingPolicy already exists for calendar {calendar.id} "
                f"in organization {org_id}."
            )
        if (
            membership_user_id is not None
            and self._policy_for_membership(membership_user_id) is not None
        ):
            raise DuplicateBookingPolicyError(
                f"A BookingPolicy already exists for membership {membership_user_id} "
                f"in organization {org_id}."
            )
        if calendar_group is not None and self._policy_for_group(calendar_group.id) is not None:
            raise DuplicateBookingPolicyError(
                f"A BookingPolicy already exists for calendar group {calendar_group.id} "
                f"in organization {org_id}."
            )
        if is_organization_default and self._org_default_policy() is not None:
            raise DuplicateBookingPolicyError(
                f"An organization-default BookingPolicy already exists for organization {org_id}."
            )

        policy = BookingPolicy.objects.create(
            organization=org,
            calendar=calendar,
            membership_user_id=membership_user_id,
            calendar_group=calendar_group,
            is_organization_default=is_organization_default,
            lead_time_seconds=lead_time_seconds,
            max_horizon_seconds=max_horizon_seconds,
            buffer_before_seconds=buffer_before_seconds,
            buffer_after_seconds=buffer_after_seconds,
        )
        self._audit_write(AuditAction.CREATE, policy)
        return policy

    def update_booking_policy(
        self,
        policy: BookingPolicy,
        *,
        lead_time_seconds: int | None = None,
        max_horizon_seconds: int | None = None,
        buffer_before_seconds: int | None = None,
        buffer_after_seconds: int | None = None,
    ) -> BookingPolicy:
        """Update the rule-fields of an existing BookingPolicy.

        Target fields (calendar, membership, calendar_group, is_organization_default)
        are intentionally not updatable — to change a target, delete and re-create.
        Emits an ``AuditService`` UPDATE record with field diffs on success.
        """
        self._assert_initialized()

        # Capture the before-state for the diff.
        before = {
            "lead_time_seconds": policy.lead_time_seconds,
            "max_horizon_seconds": policy.max_horizon_seconds,
            "buffer_before_seconds": policy.buffer_before_seconds,
            "buffer_after_seconds": policy.buffer_after_seconds,
        }

        fields_to_update: list[str] = []
        if lead_time_seconds is not None:
            policy.lead_time_seconds = lead_time_seconds
            fields_to_update.append("lead_time_seconds")
        if max_horizon_seconds is not None:
            policy.max_horizon_seconds = max_horizon_seconds
            fields_to_update.append("max_horizon_seconds")
        if buffer_before_seconds is not None:
            policy.buffer_before_seconds = buffer_before_seconds
            fields_to_update.append("buffer_before_seconds")
        if buffer_after_seconds is not None:
            policy.buffer_after_seconds = buffer_after_seconds
            fields_to_update.append("buffer_after_seconds")

        if fields_to_update:
            policy.save(update_fields=fields_to_update)

        after = {
            "lead_time_seconds": policy.lead_time_seconds,
            "max_horizon_seconds": policy.max_horizon_seconds,
            "buffer_before_seconds": policy.buffer_before_seconds,
            "buffer_after_seconds": policy.buffer_after_seconds,
        }
        diff = compute_diff(before, after)
        self._audit_write(AuditAction.UPDATE, policy, diff=diff)
        return policy

    def delete_booking_policy(self, policy: BookingPolicy | None) -> None:
        """Delete a BookingPolicy.

        Idempotent no-op when ``policy`` is ``None`` (delete-absent semantics from
        the plan's Guiding Decisions).  Emits an ``AuditService`` DELETE record
        when an actual row is deleted.
        """
        self._assert_initialized()

        if policy is None:
            return

        self._audit_write(AuditAction.DELETE, policy)
        policy.delete()

    # ------------------------------------------------------------------
    # Convenience: fetch-then-delete helpers for the REST / GraphQL layers
    # ------------------------------------------------------------------

    def delete_policy_for_calendar(self, calendar: Calendar) -> None:
        """Delete the policy attached to ``calendar``, or no-op if absent."""
        self._assert_initialized()
        policy = self._policy_for_calendar(calendar.id)
        self.delete_booking_policy(policy)

    def delete_policy_for_membership(self, membership_user_id: int) -> None:
        """Delete the policy attached to ``membership_user_id``, or no-op if absent."""
        self._assert_initialized()
        policy = self._policy_for_membership(membership_user_id)
        self.delete_booking_policy(policy)

    def delete_policy_for_group(self, calendar_group: CalendarGroup) -> None:
        """Delete the policy attached to ``calendar_group``, or no-op if absent."""
        self._assert_initialized()
        policy = self._policy_for_group(calendar_group.id)
        self.delete_booking_policy(policy)

    def delete_org_default_policy(self) -> None:
        """Delete the organization-default policy, or no-op if absent."""
        self._assert_initialized()
        policy = self._org_default_policy()
        self.delete_booking_policy(policy)

    # ------------------------------------------------------------------
    # Fetch helpers (for REST / GraphQL read paths)
    # ------------------------------------------------------------------

    def get_all_policies(self):  # type: ignore[return]
        """Return an org-scoped queryset of all BookingPolicy rows."""
        self._assert_initialized()
        return BookingPolicy.objects.filter_by_organization(self.organization.id)  # type: ignore[union-attr]
