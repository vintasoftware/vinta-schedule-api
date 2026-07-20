import datetime
from collections.abc import Iterable
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from calendar_integration.exceptions import (
    InvalidTokenError,
    TokenAlreadyUsedError,
    TokenExpiredError,
    TokenRevokedError,
)
from calendar_integration.querysets import (
    BookingPolicyQuerySet,
    CalendarEventGroupSelectionQuerySet,
    CalendarEventQuerySet,
    CalendarGroupQuerySet,
    CalendarGroupSlotMembershipQuerySet,
    CalendarGroupSlotQuerySet,
    CalendarManagementTokenQuerySet,
    CalendarQuerySet,
    CalendarSyncQuerySet,
    ExternalEventChangeRequestQuerySet,
    RecurringQuerySetMixin,
)
from organizations.managers import BaseOrganizationModelManager


if TYPE_CHECKING:
    from calendar_integration.models import BookingPolicy, CalendarManagementToken
    from organizations.models import OrganizationMembership as OrganizationMembershipType


class RecurringManagerMixin:
    """
    Mixin for managers that provides recurring functionality.
    Should be used with managers that inherit from BaseOrganizationManager.
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


class CalendarManager(BaseOrganizationModelManager):
    """
    Custom manager for Calendar model to handle specific queries.
    """

    def get_queryset(self) -> CalendarQuerySet:
        return CalendarQuerySet(self.model, using=self._db)

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


class CalendarEventManager(BaseOrganizationModelManager, RecurringManagerMixin):
    """Custom manager for CalendarEvent model to handle specific queries."""

    def get_queryset(self) -> CalendarEventQuerySet:
        return CalendarEventQuerySet(self.model, using=self._db)


class CalendarSyncManager(BaseOrganizationModelManager):
    """Custom manager for CalendarSync model to handle specific queries."""

    def get_queryset(self) -> CalendarSyncQuerySet:
        return CalendarSyncQuerySet(self.model, using=self._db)

    def get_not_started_calendar_sync(self, calendar_sync_id: int):
        """
        Retrieve a calendar sync that has not started yet.
        :param calendar_sync_id: ID of the calendar sync to retrieve.
        :return: CalendarSync instance if found, otherwise None.
        """
        return self.get_queryset().get_not_started_calendar_sync(calendar_sync_id=calendar_sync_id)


class BlockedTimeManager(BaseOrganizationModelManager, RecurringManagerMixin):
    """Custom manager for BlockedTime model to handle specific queries."""

    def get_queryset(self):
        from calendar_integration.querysets import BlockedTimeQuerySet

        return BlockedTimeQuerySet(self.model, using=self._db)


class AvailableTimeManager(BaseOrganizationModelManager, RecurringManagerMixin):
    """Custom manager for AvailableTime model to handle specific queries."""

    def get_queryset(self):
        from calendar_integration.querysets import AvailableTimeQuerySet

        return AvailableTimeQuerySet(self.model, using=self._db)

    def only_user_authored(self):
        """Wraps :meth:`AvailableTimeQuerySet.only_user_authored`."""
        return self.get_queryset().only_user_authored()


class CalendarGroupManager(BaseOrganizationModelManager):
    """Custom manager for CalendarGroup model to handle specific queries."""

    def get_queryset(self) -> CalendarGroupQuerySet:
        return CalendarGroupQuerySet(self.model, using=self._db)

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


class CalendarGroupSlotManager(BaseOrganizationModelManager):
    """Custom manager for CalendarGroupSlot model to handle specific queries."""

    def get_queryset(self) -> CalendarGroupSlotQuerySet:
        return CalendarGroupSlotQuerySet(self.model, using=self._db)


class CalendarGroupSlotMembershipManager(BaseOrganizationModelManager):
    """Custom manager for CalendarGroupSlotMembership model to handle specific queries."""

    def get_queryset(self) -> CalendarGroupSlotMembershipQuerySet:
        return CalendarGroupSlotMembershipQuerySet(self.model, using=self._db)


class CalendarEventGroupSelectionManager(BaseOrganizationModelManager):
    """Custom manager for CalendarEventGroupSelection model to handle specific queries."""

    def get_queryset(self) -> CalendarEventGroupSelectionQuerySet:
        return CalendarEventGroupSelectionQuerySet(self.model, using=self._db)


class CalendarManagementTokenManager(BaseOrganizationModelManager):
    """Manager for CalendarManagementToken with lifecycle-aware query methods."""

    def get_queryset(self) -> CalendarManagementTokenQuerySet:
        return CalendarManagementTokenQuerySet(self.model, using=self._db)

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
                locked = (
                    self.get_queryset()
                    .filter(organization_id=token.organization_id)
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


class ExternalEventChangeRequestManager(BaseOrganizationModelManager):
    """Manager for ExternalEventChangeRequest with domain-specific query methods."""

    def get_queryset(self) -> ExternalEventChangeRequestQuerySet:
        return ExternalEventChangeRequestQuerySet(self.model, using=self._db)

    def resolvable_by(
        self, membership: "OrganizationMembershipType"
    ) -> ExternalEventChangeRequestQuerySet:
        """Delegate to the queryset's ``resolvable_by`` method.

        Returns change requests the given membership is eligible to resolve.
        """
        return self.get_queryset().resolvable_by(membership)


class BookingPolicyManager(BaseOrganizationModelManager):
    """Manager for BookingPolicy exposing the per-target lookups the resolver uses.

    A policy is attached to exactly one target (calendar / membership / calendar
    group / organization default); these helpers return the single matching row
    (or ``None``) for a given target, all scoped through the inherited
    organization filter.
    """

    def get_queryset(self) -> BookingPolicyQuerySet:
        return BookingPolicyQuerySet(self.model, using=self._db)

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

        queryset = self.get_queryset().filter_by_organization(organization_id)
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
        return self.get_queryset().filter_by_organization(organization_id).org_default().first()
