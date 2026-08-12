import datetime
from collections.abc import Iterable
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from organizations.managers import SingleOrganizationModelManager

from calendar_integration.exceptions import (
    InvalidTokenError,
    TokenAlreadyUsedError,
    TokenExpiredError,
    TokenRevokedError,
)
from calendar_integration.querysets import (
    AvailableTimeQuerySet,
    BlockedTimeQuerySet,
    BookingPolicyQuerySet,
    CalendarEventGroupSelectionQuerySet,
    CalendarEventQuerySet,
    CalendarGroupQuerySet,
    CalendarGroupSlotMembershipQuerySet,
    CalendarGroupSlotQuerySet,
    CalendarGroupSlotQuotaRuleQuerySet,
    CalendarManagementTokenQuerySet,
    CalendarQuerySet,
    CalendarSyncQuerySet,
    ExternalEventChangeRequestQuerySet,
    RecurringQuerySetMixin,
)


if TYPE_CHECKING:
    from calendar_integration.models import BookingPolicy, CalendarManagementToken
    from tenancy.models import OrganizationMembership as OrganizationMembershipType


class OrganizationScopedManager(SingleOrganizationModelManager):
    """Default manager for every organization-scoped model in this app.

    Reads are the package's: ``objects`` scopes to the organization bound to
    the current context, and under ``STRICT_ORGANIZATION_FILTER`` an unbound
    read raises instead of quietly returning nothing.

    Writes are not. ``create`` / ``get_or_create`` / ``update_or_create`` /
    ``bulk_create`` are generated onto the manager from ``QuerySet``, so they
    all route through ``get_queryset()`` and inherit that refusal -- but the
    scope they are refusing to resolve has no effect on what they do:
    ``QuerySet.create`` does not carry the queryset's filters onto the new row,
    and ``bulk_create`` takes fully-built instances. Refusing
    ``Calendar.objects.create(organization=org, ...)`` -- which is how every
    write in this codebase is spelled, and which was *required* to name its
    organization under the manager this replaces -- would reject a statement
    that is already unambiguous.

    So a write that names its organization goes to the unscoped queryset, and a
    write that does not still goes through the scoped one: either the context
    supplies the organization (and ``SingleOrganizationModelMixin.save()``
    stamps it) or nothing does and it raises, exactly as a read would.

    Related-object access is not scoped either -- see :meth:`get_queryset`.
    """

    #: Ways a caller can name the organization on a write. ``organization_id``
    #: is accepted because ``create(organization_id=...)`` is as explicit as
    #: passing the instance and appears throughout the services.
    _ORGANIZATION_KWARGS = ("organization", "organization_id")

    def get_queryset(self, *args, **kwargs):
        """Scope to the bound organization -- unless this is a related manager.

        Django builds the reverse accessor for a relation
        (``event.attendances``, ``calendar.syncs``, ``group.slots``) by
        subclassing the target model's ``_default_manager`` class, so without
        this every one of them would demand a bound organization on top of the
        parent row it is already restricted to.

        There is nothing for the context to add there, and something for it to
        take away:

        * The queryset is filtered to one parent instance, and for a relation
          declared with ``OrganizationSafeForeignKey`` that filter is on
          ``(<name>_fk, organization)`` -- the organization is in the ``WHERE``
          clause already, taken from the parent row rather than from ambient
          state. A second, ambient organization condition can only ever be
          redundant (same organization) or empty the result (different one),
          and the second case means the caller already holds an object from an
          organization it is not scoped to, which is the bug -- reported at the
          traversal instead of where it happened.
        * Django itself takes this position for *forward* relations, which go
          through ``_base_manager`` precisely so a related object can always be
          retrieved; the reverse side using ``_default_manager`` is the
          documented exception, with a documented warning that a filtering
          default manager hides rows.

        Detected by ``instance``, which Django's generated related managers set
        in ``__init__`` and a plain model manager never has.
        """
        if getattr(self, "instance", None) is not None:
            return self.get_original_queryset(*args, **kwargs)
        return super().get_queryset(*args, **kwargs)

    def _names_an_organization(self, kwargs: dict) -> bool:
        return any(kwargs.get(name) is not None for name in self._ORGANIZATION_KWARGS)

    def create(self, **kwargs):
        if self._names_an_organization(kwargs):
            return self.unscoped().create(**kwargs)
        return super().create(**kwargs)

    def get_or_create(self, defaults=None, **kwargs):
        if self._names_an_organization(kwargs) or self._names_an_organization(defaults or {}):
            return self.unscoped().get_or_create(defaults=defaults, **kwargs)
        return super().get_or_create(defaults=defaults, **kwargs)

    def update_or_create(self, defaults=None, **kwargs):
        if self._names_an_organization(kwargs) or self._names_an_organization(defaults or {}):
            return self.unscoped().update_or_create(defaults=defaults, **kwargs)
        return super().update_or_create(defaults=defaults, **kwargs)

    def bulk_create(self, objs, *args, **kwargs):
        # Always unscoped: every object carries its own ``organization``, and an
        # object that does not fails the column's NOT NULL rather than silently
        # landing in whichever organization happened to be bound. ``save()``'s
        # context fallback is not involved -- ``bulk_create`` does not call it.
        return self.unscoped().bulk_create(objs, *args, **kwargs)


class RecurringManagerMixin:
    """
    Mixin for managers that provides recurring functionality.
    Should be used with managers built from ``SingleOrganizationModelManager``.
    The QuerySet should also inherit from RecurringQuerySetMixin.
    """

    def get_queryset(self) -> RecurringQuerySetMixin:
        raise NotImplementedError("Concrete managers must implement get_queryset")

    def annotate_recurring_occurrences_on_date_range(
        self,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        max_occurrences=10000,
        overlap=False,
    ):
        """
        Annotate objects with their recurring occurrences in the date range.
        Delegates to the queryset implementation.
        """
        return self.get_queryset().annotate_recurring_occurrences_on_date_range(
            start_date, end_date, max_occurrences, overlap=overlap
        )

    def annotate_recurring_occurrences_with_bulk_modifications_on_date_range(
        self, start_date: datetime.datetime, end_date: datetime.datetime, max_occurrences=10000
    ):
        """
        Annotate objects with their recurring occurrences in the date range, including bulk modifications.
        Delegates to the queryset implementation.
        """
        return self.get_queryset().annotate_recurring_occurrences_with_bulk_modifications_on_date_range(
            start_date, end_date, max_occurrences
        )

    def filter_master_recurring_objects(self):
        """Filter to get only master recurring objects (not instances)."""
        return self.get_queryset().filter_master_recurring_objects()

    def filter_recurring_instances(self):
        """Filter to get only recurring instances (not masters)."""
        return self.get_queryset().filter_recurring_instances()

    def filter_recurring_objects(self):
        """Filter to get objects that have recurrence rules."""
        return self.get_queryset().filter_recurring_objects()

    def filter_non_recurring_objects(self):
        """Filter to get objects that don't have recurrence rules."""
        return self.get_queryset().filter_non_recurring_objects()

    def get_occurrences_in_range_with_bulk_modifications(
        self,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        include_continuations: bool = True,
        max_occurrences: int = 10000,
    ):
        """
        Get occurrences considering bulk modifications.
        Delegates to the queryset implementation.
        """
        return self.get_queryset().get_occurrences_in_range_with_bulk_modifications(
            start_date, end_date, include_continuations, max_occurrences
        )


class CalendarManager(OrganizationScopedManager.from_queryset(CalendarQuerySet)):  # type: ignore[misc]
    """
    Custom manager for Calendar model to handle specific queries.
    """

    def live_of_type(self, calendar_type: str) -> CalendarQuerySet:
        """Wraps :meth:`CalendarQuerySet.live_of_type`."""
        return self.get_queryset().live_of_type(calendar_type)

    def only_virtual_calendars(self):
        """
        Returns all virtual calendars.
        """
        return self.get_queryset().filter_by_is_virtual()

    def only_resource_calendars(self):
        """
        Returns all resource calendars.
        """
        return self.get_queryset().filter_by_is_resource()

    def only_calendars_by_provider(self, provider):
        """
        Returns calendars filtered by the specified provider.
        """
        return self.get_queryset().only_calendars_by_provider(provider=provider)

    def prefetch_latest_sync(self):
        """
        Prefetches the latest sync record for each calendar.
        """
        return self.get_queryset().prefetch_latest_sync()

    def only_calendars_available_in_ranges(
        self, ranges: Iterable[tuple[datetime.datetime, datetime.datetime]]
    ):
        """
        Returns calendars that are available in the specified date range.
        :param start_datetime: Start of the date range.
        :param end_datetime: End of the date range.
        :return: QuerySet of calendars available in the specified range.
        """
        return self.get_queryset().only_calendars_available_in_ranges(ranges=ranges)

    def only_calendars_available_in_ranges_with_bulk_modifications(
        self, ranges: Iterable[tuple[datetime.datetime, datetime.datetime]]
    ):
        """
        Same as `only_calendars_available_in_ranges` but expands recurring events
        through their bulk-modification continuations.
        """
        return self.get_queryset().only_calendars_available_in_ranges_with_bulk_modifications(
            ranges=ranges
        )

    def annotate_effective_policy(self) -> CalendarQuerySet:
        """Annotate the four ``effective_*_seconds`` booking-policy columns.

        Delegates to the queryset; resolves the whole-policy precedence chain
        (calendar → owning-membership → org-default → unconstrained) in SQL.
        """
        return self.get_queryset().annotate_effective_policy()


class CalendarEventManager(  # type: ignore[misc]
    OrganizationScopedManager.from_queryset(CalendarEventQuerySet), RecurringManagerMixin
):
    """Custom manager for CalendarEvent model to handle specific queries."""

    def occurrence_bearing_masters_in_range(
        self, start: datetime.datetime, end: datetime.datetime
    ) -> CalendarEventQuerySet:
        """Wraps :meth:`CalendarEventQuerySet.occurrence_bearing_masters_in_range`."""
        return self.get_queryset().occurrence_bearing_masters_in_range(start, end)


class CalendarSyncManager(OrganizationScopedManager.from_queryset(CalendarSyncQuerySet)):  # type: ignore[misc]
    """Custom manager for CalendarSync model to handle specific queries."""

    def get_not_started_calendar_sync(self, calendar_sync_id: int):
        """
        Retrieve a calendar sync that has not started yet.
        :param calendar_sync_id: ID of the calendar sync to retrieve.
        :return: CalendarSync instance if found, otherwise None.
        """
        return self.get_queryset().get_not_started_calendar_sync(calendar_sync_id=calendar_sync_id)


class BlockedTimeManager(  # type: ignore[misc]
    OrganizationScopedManager.from_queryset(BlockedTimeQuerySet), RecurringManagerMixin
):
    """Custom manager for BlockedTime model to handle specific queries.

    ``group_slot`` scoping (``CALENDAR_GROUP_SCOPED_AVAILABILITY`` Phase 0):
    :meth:`get_queryset` — and therefore every plain ``.objects`` call — returns
    only base rows (``group_slot IS NULL``), which is today's behavior and every
    existing call site's implicit expectation. Group-scoped rows are reachable
    only through :meth:`for_group_slot` or :meth:`unscoped`, both explicit
    opt-in accessors.

    Group-slot scoping is applied to all three of the package's *scoping* entry
    points, not just ``get_queryset``. ``filter_by_organization`` and
    ``exclude_by_organization`` start from the **unscoped** queryset by design
    (reaching another organization's rows is the only reason to call them), so
    inheriting them unchanged would have made the single most common call in
    this codebase — ``BlockedTime.objects.filter_by_organization(org)`` — start
    returning group-scoped rows it has never returned. ``unscoped()`` is
    deliberately *not* overridden: the package's version already means exactly
    what this manager's own ``unscoped()`` used to (every row, no filter of any
    kind), so the two names collapsed into one with no call site change.
    """

    def get_queryset(self, *args, **kwargs) -> "BlockedTimeQuerySet":
        return super().get_queryset(*args, **kwargs).base_rows_only()

    def filter_by_organization(self, organization, *args, **kwargs) -> "BlockedTimeQuerySet":
        return super().filter_by_organization(organization, *args, **kwargs).base_rows_only()

    def exclude_by_organization(self, organization, *args, **kwargs) -> "BlockedTimeQuerySet":
        return super().exclude_by_organization(organization, *args, **kwargs).base_rows_only()

    def for_group_slot(self, group_slot_id: int) -> "BlockedTimeQuerySet":
        """Explicit opt-in: only the blocked-time rows scoped to one ``CalendarGroupSlot``.

        Deliberately cross-organization (it starts from ``unscoped()``): every
        caller narrows with ``filter_by_organization`` on the returned queryset,
        which is where the tenant boundary is drawn.
        """
        return self.unscoped().for_group_slot(group_slot_id)


class AvailableTimeManager(  # type: ignore[misc]
    OrganizationScopedManager.from_queryset(AvailableTimeQuerySet), RecurringManagerMixin
):
    """Custom manager for AvailableTime model to handle specific queries.

    ``group_slot`` scoping (``CALENDAR_GROUP_SCOPED_AVAILABILITY`` Phase 0):
    :meth:`get_queryset` — and therefore every plain ``.objects`` call — returns
    only base rows (``group_slot IS NULL``), which is today's behavior and every
    existing call site's implicit expectation. Group-scoped rows are reachable
    only through :meth:`for_group_slot` or :meth:`unscoped`, both explicit
    opt-in accessors.

    See :class:`BlockedTimeManager` for why the group-slot filter is applied to
    ``filter_by_organization`` / ``exclude_by_organization`` as well as to
    ``get_queryset``, and why ``unscoped()`` is left to the package.
    """

    def get_queryset(self, *args, **kwargs) -> "AvailableTimeQuerySet":
        return super().get_queryset(*args, **kwargs).base_rows_only()

    def filter_by_organization(self, organization, *args, **kwargs) -> "AvailableTimeQuerySet":
        return super().filter_by_organization(organization, *args, **kwargs).base_rows_only()

    def exclude_by_organization(self, organization, *args, **kwargs) -> "AvailableTimeQuerySet":
        return super().exclude_by_organization(organization, *args, **kwargs).base_rows_only()

    def for_group_slot(self, group_slot_id: int) -> "AvailableTimeQuerySet":
        """Explicit opt-in: only the availability rows scoped to one ``CalendarGroupSlot``.

        Deliberately cross-organization (it starts from ``unscoped()``): every
        caller narrows with ``filter_by_organization`` on the returned queryset,
        which is where the tenant boundary is drawn.
        """
        return self.unscoped().for_group_slot(group_slot_id)

    def only_user_authored(self):
        """Wraps :meth:`AvailableTimeQuerySet.only_user_authored`."""
        return self.get_queryset().only_user_authored()


class CalendarGroupManager(OrganizationScopedManager.from_queryset(CalendarGroupQuerySet)):  # type: ignore[misc]
    """Custom manager for CalendarGroup model to handle specific queries."""

    def only_member_of(self, membership_user_id: int) -> CalendarGroupQuerySet:
        """Wraps :meth:`CalendarGroupQuerySet.only_member_of`."""
        return self.get_queryset().only_member_of(membership_user_id)

    def only_groups_bookable_in_ranges(
        self, ranges: Iterable[tuple[datetime.datetime, datetime.datetime]]
    ):
        """
        Returns groups where every slot has at least `required_count` calendars
        from its pool available in every requested range.
        """
        return self.get_queryset().only_groups_bookable_in_ranges(ranges=ranges)

    def only_groups_bookable_in_ranges_with_bulk_modifications(
        self, ranges: Iterable[tuple[datetime.datetime, datetime.datetime]]
    ):
        """
        Same as `only_groups_bookable_in_ranges` but expands recurring events
        through their bulk-modification continuations when computing calendar
        availability per slot.
        """
        return self.get_queryset().only_groups_bookable_in_ranges_with_bulk_modifications(
            ranges=ranges
        )

    def annotate_effective_policy(self) -> CalendarGroupQuerySet:
        """Annotate the four ``effective_*_seconds`` booking-policy columns.

        Delegates to the queryset; resolves the group precedence chain (explicit
        group policy → most_restrictive across participant calendars →
        unconstrained) in SQL.
        """
        return self.get_queryset().annotate_effective_policy()


class CalendarGroupSlotManager(OrganizationScopedManager.from_queryset(CalendarGroupSlotQuerySet)):  # type: ignore[misc]
    """Custom manager for CalendarGroupSlot model to handle specific queries."""


class CalendarGroupSlotMembershipManager(
    OrganizationScopedManager.from_queryset(CalendarGroupSlotMembershipQuerySet)
):  # type: ignore[misc]
    """Custom manager for CalendarGroupSlotMembership model to handle specific queries."""


class CalendarEventGroupSelectionManager(
    OrganizationScopedManager.from_queryset(CalendarEventGroupSelectionQuerySet)
):  # type: ignore[misc]
    """Custom manager for CalendarEventGroupSelection model to handle specific queries."""


class CalendarGroupSlotQuotaRuleManager(
    OrganizationScopedManager.from_queryset(CalendarGroupSlotQuotaRuleQuerySet)
):  # type: ignore[misc]
    """Custom manager for CalendarGroupSlotQuotaRule model to handle specific queries."""

    def for_group_slot(self, group_slot_id: int) -> CalendarGroupSlotQuotaRuleQuerySet:
        """Wraps :meth:`CalendarGroupSlotQuotaRuleQuerySet.for_group_slot`."""
        return self.get_queryset().for_group_slot(group_slot_id)


class CalendarManagementTokenManager(
    OrganizationScopedManager.from_queryset(CalendarManagementTokenQuerySet)
):  # type: ignore[misc]
    """Manager for CalendarManagementToken with lifecycle-aware query methods."""

    def active(self) -> CalendarManagementTokenQuerySet:
        """Return tokens that are not used, not revoked, and not expired."""
        return self.get_queryset().active()

    def consume(self, token: "CalendarManagementToken", source_ip: str) -> None:
        """Atomically consume *token* by setting used_at + consumed_source_ip.

        Wraps the lock + re-check + save in ``transaction.atomic()`` so the
        SELECT FOR UPDATE lock is always acquired inside a transaction,
        regardless of the caller's ambient context (request, Celery task, or
        management command). ``atomic()`` is reentrant — it is a no-op when a
        request transaction (ATOMIC_REQUESTS) already exists. Uses SELECT FOR
        UPDATE to serialise concurrent consume attempts — the first caller wins;
        subsequent callers receive TokenAlreadyUsedError.

        Args:
            token: The CalendarManagementToken instance to consume.
            source_ip: The IP address of the consuming client.

        Raises:
            InvalidTokenError: If no token resolves for (organization_id, pk).
            TokenExpiredError: If the token has expired.
            TokenAlreadyUsedError: If the token was already used (including by a
                concurrent transaction that committed first).
            TokenRevokedError: If the token has been revoked.
        """
        with transaction.atomic():
            # Re-fetch under a row-level lock to serialise concurrent consume calls.
            try:
                # ``filter_by_organization`` rather than the implicit scope:
                # ``consume`` is reached from the public token endpoints, which
                # authenticate by token rather than by member, so nothing has
                # bound an organization -- the token's own organization is the
                # one to scope to, and it is what the lookup must match.
                locked = (
                    self.filter_by_organization(token.organization_id)
                    .select_for_update()
                    .get(pk=token.pk)
                )
            except self.model.DoesNotExist as exc:
                raise InvalidTokenError() from exc

            now = timezone.now()

            if locked.revoked_at is not None:
                raise TokenRevokedError()

            if locked.used_at is not None:
                raise TokenAlreadyUsedError()

            if locked.expires_at is not None and locked.expires_at <= now:
                raise TokenExpiredError()

            locked.used_at = now
            locked.consumed_source_ip = source_ip
            locked.save(update_fields=["used_at", "consumed_source_ip"])

    def get_token_error_code(self, token: "CalendarManagementToken") -> str | None:
        """Return a machine-readable error code if the token is in a terminal state.

        Returns None when the token is active (no error).

        This method does NOT acquire a lock — it is safe to call for read-only
        validation where atomicity is not required (e.g. resolvers for
        availability reads).

        Returns:
            ``"REVOKED"`` / ``"ALREADY_USED"`` / ``"EXPIRED"`` or ``None``.
        """
        if token.revoked_at is not None:
            return "REVOKED"
        if token.used_at is not None:
            return "ALREADY_USED"
        if token.expires_at is not None and token.expires_at <= timezone.now():
            return "EXPIRED"
        return None


class ExternalEventChangeRequestManager(
    OrganizationScopedManager.from_queryset(ExternalEventChangeRequestQuerySet)
):  # type: ignore[misc]
    """Manager for ExternalEventChangeRequest with domain-specific query methods."""

    def resolvable_by(
        self, membership: "OrganizationMembershipType"
    ) -> ExternalEventChangeRequestQuerySet:
        """Delegate to the queryset's ``resolvable_by`` method.

        Returns change requests the given membership is eligible to resolve.
        """
        return self.get_queryset().resolvable_by(membership)


class BookingPolicyManager(OrganizationScopedManager.from_queryset(BookingPolicyQuerySet)):  # type: ignore[misc]
    """Manager for BookingPolicy exposing the per-target lookups the resolver uses.

    A policy is attached to exactly one target (calendar / membership / calendar
    group / organization default); these helpers return the single matching row
    (or ``None``) for a given target, all scoped through the inherited
    organization filter.
    """

    def for_target(
        self,
        organization_id: int,
        *,
        calendar_id: int | None = None,
        membership_user_id: int | None = None,
        calendar_group_id: int | None = None,
    ) -> "BookingPolicy | None":
        """Return the policy attached to exactly one of the given targets, or ``None``.

        Scoped to ``organization_id`` via ``filter_by_organization`` (required —
        the base queryset refuses to evaluate without an organization filter).
        Exactly one of ``calendar_id`` / ``membership_user_id`` /
        ``calendar_group_id`` must be provided. The per-target partial unique
        indexes guarantee at most one matching row.
        """
        provided = [
            value
            for value in (calendar_id, membership_user_id, calendar_group_id)
            if value is not None
        ]
        if len(provided) != 1:
            raise ValueError(
                "for_target requires exactly one of calendar_id, membership_user_id, "
                "or calendar_group_id."
            )

        queryset = self.filter_by_organization(organization_id)
        if calendar_id is not None:
            queryset = queryset.for_calendar(calendar_id)
        elif membership_user_id is not None:
            queryset = queryset.for_membership(membership_user_id)
        else:
            queryset = queryset.for_calendar_group(calendar_group_id)  # type: ignore[arg-type]

        return queryset.first()

    def org_default(self, organization_id: int) -> "BookingPolicy | None":
        """Return the organization-default policy, or ``None`` if none is set.

        Scoped to ``organization_id`` via ``filter_by_organization`` (required —
        the base queryset refuses to evaluate without an organization filter).
        """
        return self.filter_by_organization(organization_id).org_default().first()
