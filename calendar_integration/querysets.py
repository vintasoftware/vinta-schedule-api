import datetime
from collections.abc import Iterable
from typing import TYPE_CHECKING

from django.db.models import (
    Case,
    Count,
    Exists,
    F,
    IntegerField,
    Max,
    Min,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from calendar_integration.constants import (
    CalendarSyncStatus,
    CalendarType,
    CalendarVisibility,
    ExternalEventChangeRequestStatus,
)
from calendar_integration.database_functions import (
    GetAvailableTimeOccurrencesJSON,
    GetAvailableTimeOccurrencesWithBulkModificationsJSON,
    GetBlockedTimeOccurrencesJSON,
    GetBlockedTimeOccurrencesWithBulkModificationsJSON,
    GetEventOccurrencesJSON,
    GetEventOccurrencesWithBulkModificationsJSON,
)
from organizations.querysets import BaseOrganizationModelQuerySet


if TYPE_CHECKING:
    from calendar_integration.models import CalendarEvent as CalendarEventType
    from calendar_integration.models import CalendarSync as CalendarSyncType
    from organizations.models import OrganizationMembership as OrganizationMembershipType


class CalendarManagementTokenQuerySet(BaseOrganizationModelQuerySet):
    """QuerySet for CalendarManagementToken with lifecycle-aware filtering."""

    def active(self) -> "CalendarManagementTokenQuerySet":
        """Return tokens that are not used, not revoked, and not expired.

        A token is active when all three conditions hold:
          - ``used_at`` is NULL (never consumed),
          - ``revoked_at`` is NULL (not revoked),
          - ``expires_at`` is NULL OR ``expires_at`` is in the future.
        """
        now = timezone.now()
        return self.filter(
            used_at__isnull=True,
            revoked_at__isnull=True,
        ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))


class RecurringQuerySetMixin:
    """
    Mixin for querysets that provides recurring functionality.
    Should be used with querysets that inherit from BaseOrganizationModelQuerySet.
    """

    def annotate_recurring_occurrences_on_date_range(
        self, start_date: datetime.datetime, end_date: datetime.datetime, max_occurrences=10000
    ):
        """
        Annotate objects with their recurring occurrences in the date range.
        This method should be overridden by concrete querysets to use their specific database function.
        """
        raise NotImplementedError(
            "Concrete querysets must implement annotate_recurring_occurrences_on_date_range"
        )

    def filter_master_recurring_objects(self):
        """Filter to get only master recurring objects (not instances)."""
        return self.filter(parent_recurring_object__isnull=True, recurrence_rule__isnull=False)

    def filter_recurring_instances(self):
        """Filter to get only recurring instances (not masters)."""
        return self.filter(parent_recurring_object__isnull=False)

    def filter_recurring_objects(self):
        """Filter to get objects that have recurrence rules."""
        return self.filter(recurrence_rule__isnull=False)

    def filter_non_recurring_objects(self):
        """Filter to get objects that don't have recurrence rules."""
        return self.filter(recurrence_rule__isnull=True)

    def annotate_recurring_occurrences_with_bulk_modifications_on_date_range(
        self, start_date: datetime.datetime, end_date: datetime.datetime, max_occurrences=10000
    ):
        """
        Annotate objects with their recurring occurrences including bulk modifications in the date range.
        This method should be overridden by concrete querysets to use their specific bulk modification database function.
        """
        raise NotImplementedError(
            "Concrete querysets must implement annotate_recurring_occurrences_with_bulk_modifications_on_date_range"
        )

    def get_occurrences_in_range_with_bulk_modifications(
        self,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        include_continuations: bool = True,
        max_occurrences: int = 10000,
    ):
        """
        Get occurrences considering bulk modifications across all objects in the queryset.

        This method efficiently aggregates occurrences from:
        1. All objects in the queryset (potentially truncated by bulk modifications)
        2. Their continuation objects created by bulk modifications

        Args:
            start_date: Start of the date range
            end_date: End of the date range
            include_continuations: Whether to include occurrences from continuation objects
            max_occurrences: Maximum number of occurrences to return per object

        Returns:
            QuerySet of all occurrence instances, ordered by start time
        """
        all_objects = []

        # Get all recurring objects in the queryset
        recurring_objects = self.filter_recurring_objects()

        # For each recurring object, get its occurrences
        for obj in recurring_objects:
            occurrences = obj.get_occurrences_in_range_with_bulk_modifications(
                start_date=start_date,
                end_date=end_date,
                include_continuations=include_continuations,
                max_occurrences=max_occurrences,
            )
            all_objects.extend(occurrences)

        # Sort all occurrences by start time
        all_objects.sort(key=lambda occurrence: occurrence.start_time)

        # Convert to a queryset if possible, otherwise return as list
        if all_objects:
            # Get all IDs and return as a queryset ordered by start_time
            object_ids = [obj.id for obj in all_objects if hasattr(obj, "id")]
            if object_ids:
                # Create a case-when ordering to preserve the sorted order
                from django.db.models import Case, IntegerField, When

                preserved_order = Case(
                    *[When(pk=pk, then=pos) for pos, pk in enumerate(object_ids)],
                    output_field=IntegerField(),
                )
                return self.filter(id__in=object_ids).order_by(preserved_order)  # type: ignore

        # Return empty queryset if no occurrences found
        return self.none()  # type: ignore


class CalendarQuerySet(BaseOrganizationModelQuerySet):
    """
    Custom QuerySet for Calendar model to handle specific queries.
    """

    def exclude_inactive(self):
        """Exclude soft-deleted calendars (visibility=inactive). Includes active and unlisted."""
        return self.exclude(visibility=CalendarVisibility.INACTIVE)

    def live_of_type(self, calendar_type: str) -> "CalendarQuerySet":
        """Calendars of ``calendar_type`` that still exist from the user's point of view.

        ``DELETE /calendars/{id}/`` sets ``visibility=INACTIVE`` rather than removing
        the row, so "how many resource calendars does this organization have" must
        exclude that state or deleting a room never frees anything. Keeping the
        soft-delete definition next to the model that defines it, rather than
        restating it in each consumer.
        """
        return self.filter(calendar_type=calendar_type).exclude_inactive()

    def not_newly_counted_as_type(self, calendar_type: str) -> "CalendarQuerySet":
        """Calendars an upsert that *forces* ``calendar_type`` would not newly count.

        The complement of *newly entering* :meth:`live_of_type` -- not of
        :meth:`live_of_type` itself; a row that is already some other live,
        non-``calendar_type`` type (e.g. a live ``PERSONAL``/``ACTIVE`` calendar) is in
        neither set. :meth:`live_of_type` is the predicate the ``resource_calendars`` /
        ``bundle_calendars`` usage counters (``payments.services.entitlement_service``)
        count with. It lives here, next to ``live_of_type``, precisely so the two cannot
        drift apart: a bulk upsert that
        splits "already imported" from "new" using a *different* predicate than the
        counter it is guarding lets rows through unmetered, which is exactly what the
        room-import writer did before this method existed.

        A row is newly counted iff, after the upsert, it enters ``live_of_type``
        without having been in it before — i.e. iff it is live *and* currently some
        other type (a **promotion** into the counted set). So the rows that are *not*
        newly counted are:

        * already ``calendar_type`` (a true update — it was counted before and stays
          counted), and
        * soft-deleted (``visibility=INACTIVE``) — the upsert leaves ``visibility``
          untouched, so the row stays outside ``live_of_type`` afterwards and the
          count does not move.

        Everything else must consume headroom.
        """
        return self.filter(
            Q(calendar_type=calendar_type) | Q(visibility=CalendarVisibility.INACTIVE)
        )

    def external_ids_not_newly_counted_as_type(
        self, external_ids: Iterable[str], calendar_type: str
    ) -> set[str]:
        """``external_id``s among ``external_ids`` whose upsert to ``calendar_type`` is free.

        The set complement of "consumes headroom" for a bulk upsert keyed on
        ``external_id``: an id with no row at all is absent from the result (it is a
        create), and an id whose row is covered by :meth:`not_newly_counted_as_type`
        is present (the write cannot increase the counted total).
        """
        return set(
            self.filter(external_id__in=list(external_ids))
            .not_newly_counted_as_type(calendar_type)
            .values_list("external_id", flat=True)
        )

    def only_listed(self):
        """Return only calendars visible in booking/public queries (visibility=active)."""
        return self.filter(visibility=CalendarVisibility.ACTIVE)

    def filter_by_is_virtual(self, is_virtual=True):
        """
        Returns virtual calendars when is_virtual=True, or non-virtual calendars when is_virtual=False.
        """
        if is_virtual:
            return self.filter(calendar_type=CalendarType.VIRTUAL)
        else:
            return self.exclude(calendar_type=CalendarType.VIRTUAL)

    def filter_by_is_resource(self, is_resource=True):
        """
        Returns resource calendars when is_resource=True, or non-resource calendars when is_resource=False.
        """
        if is_resource:
            return self.filter(calendar_type=CalendarType.RESOURCE)
        else:
            return self.exclude(calendar_type=CalendarType.RESOURCE)

    def only_calendars_by_provider(self, provider):
        """
        Returns calendars filtered by the specified provider.
        """
        return self.filter(provider=provider)

    def only_resource_calendars(self):
        """
        Returns only resource calendars.
        """
        return self.filter_by_is_resource(True)

    def only_virtual_calendars(self):
        """
        Returns only virtual calendars.
        """
        return self.filter_by_is_virtual(True)

    def prefetch_latest_sync(self):
        """
        Prefetches the latest sync record for each calendar.
        """
        from calendar_integration.models import CalendarSync

        return self.prefetch_related(
            Prefetch(
                "syncs",
                CalendarSync.objects.filter(
                    should_update_events=True,
                    id__in=Subquery(
                        CalendarSync.objects.filter(
                            should_update_events=True,
                            calendar_fk_id=OuterRef("calendar_fk_id"),
                            organization_id=OuterRef("organization_id"),
                        )
                        .order_by("-start_datetime")
                        .values("id")[:1]
                    ),
                ),
                to_attr="_latest_sync",
            )
        )

    def update(self, **kwargs):
        # find model fields that are CalendarOrganizationForeignKey
        foreign_key_fields_in_kwargs = [
            field.name
            for field in self._meta.get_fields()
            if (
                self.model.is_field_organization_foreign_key(field)
                and (field.name in kwargs.keys() or f"{field.name}_id" in kwargs.keys())
            )
        ]

        for field_name in foreign_key_fields_in_kwargs:
            if field_name in kwargs.keys() and not kwargs.get(f"{field_name}_fk", None):
                kwargs[f"{field_name}_fk"] = kwargs.pop(field_name)
                continue
            if f"{field_name}_id" in kwargs.keys() and not kwargs.get(f"{field_name}_fk_id", None):
                kwargs[f"{field_name}_fk_id"] = kwargs.pop(f"{field_name}_id")
                continue
        return super().update(**kwargs)

    def only_calendars_available_in_ranges(
        self, ranges: Iterable[tuple[datetime.datetime, datetime.datetime]]
    ):
        """
        Returns calendars that have available time windows in all specified ranges.
        """
        return self._only_calendars_available_in_ranges(ranges, with_bulk_modifications=False)

    def only_calendars_available_in_ranges_with_bulk_modifications(
        self, ranges: Iterable[tuple[datetime.datetime, datetime.datetime]]
    ):
        """
        Same as `only_calendars_available_in_ranges`, but recurring events and
        blocked times are expanded via their bulk-modification continuation
        series (`annotate_recurring_occurrences_with_bulk_modifications_on_date_range`).
        Use this when the caller has split a recurring series with a bulk
        modification and needs the continuation occurrences to count against
        availability.
        """
        return self._only_calendars_available_in_ranges(ranges, with_bulk_modifications=True)

    def _only_calendars_available_in_ranges(
        self,
        ranges: Iterable[tuple[datetime.datetime, datetime.datetime]],
        *,
        with_bulk_modifications: bool,
    ):
        from calendar_integration.models import AvailableTime, BlockedTime, CalendarEvent

        if not ranges:
            return self.none()

        queries = []
        for start_datetime, end_datetime in ranges:
            # For managed calendars: must have available time exactly matching the range
            managed_query = Q(
                manage_available_windows=True,
                id__in=Subquery(
                    AvailableTime.objects.filter(
                        calendar_fk_id=OuterRef("id"),
                        start_time__lte=start_datetime,
                        end_time__gte=end_datetime,
                    )
                    .values("calendar_fk_id")
                    .distinct()
                ),
            )

            if with_bulk_modifications:
                events_qs = CalendarEvent.objects.annotate_recurring_occurrences_with_bulk_modifications_on_date_range(
                    start_datetime, end_datetime
                )
                recurring_occurrences_field = "recurring_occurrences"
            else:
                events_qs = CalendarEvent.objects.annotate_recurring_occurrences_on_date_range(
                    start_datetime, end_datetime
                )
                recurring_occurrences_field = "recurring_occurrences"

            # For unmanaged calendars: must NOT have conflicting events or blocked times
            unmanaged_query = Q(
                manage_available_windows=False,
            ) & ~Q(
                Q(
                    id__in=Subquery(
                        events_qs.filter(
                            Q(start_time__range=(start_datetime, end_datetime))
                            | Q(end_time__range=(start_datetime, end_datetime))
                            | Q(start_time__lte=start_datetime, end_time__gte=end_datetime)
                            | Q(**{f"{recurring_occurrences_field}__len__gt": 0}),
                            calendar_fk_id=OuterRef("id"),
                        )
                        .values("calendar_fk_id")
                        .distinct()
                    )
                )
                | Q(
                    id__in=Subquery(
                        BlockedTime.objects.filter(
                            Q(start_time__range=(start_datetime, end_datetime))
                            | Q(end_time__range=(start_datetime, end_datetime))
                            | Q(start_time__lte=start_datetime, end_time__gte=end_datetime),
                            calendar_fk_id=OuterRef("id"),
                        )
                        .values("calendar_fk_id")
                        .distinct()
                    )
                )
            )

            # Combine both conditions
            range_query = managed_query | unmanaged_query
            queries.append(range_query)

        # All ranges must be satisfied (AND operation)
        combined_query = queries[0]
        for query in queries[1:]:
            combined_query &= query

        return self.filter(combined_query)

    def annotate_effective_policy(self) -> "CalendarQuerySet":
        """Annotate the four ``effective_*_seconds`` booking-policy columns.

        Resolves, in a single query, the whole-policy precedence chain for each
        calendar — calendar policy → owning-membership policy → org-default →
        unconstrained — entirely in SQL. The first existing layer wins and all
        four of its fields are read together (no per-field COALESCE across
        layers). Decode the result with ``EffectivePolicy.from_annotation(row)``.

        The chain is org-scoped: every inner subquery filters on the calendar's
        own ``organization_id``, so the annotation is safe on a queryset spanning
        a single tenant (the manager always pre-filters by organization).
        """
        qs = self.annotate(
            _owning_membership_uid=_owning_membership_uid_expression(
                OuterRef("pk"), OuterRef("organization_id")
            )
        )
        qs = qs.annotate(
            _effective_policy_id=_winning_calendar_policy_id_expression(
                OuterRef("pk"),
                OuterRef("organization_id"),
                OuterRef("_owning_membership_uid"),
            )
        )
        return qs.annotate(
            **_policy_field_subqueries(
                OuterRef("_effective_policy_id"), OuterRef("organization_id")
            )
        )


def _owning_membership_uid_expression(calendar_id_ref: OuterRef, org_id_ref: OuterRef) -> Coalesce:
    """Resolve, in SQL, the single owning-membership user id for a calendar.

    Matches ``BookingPolicyService._resolve_owning_membership_user_id`` exactly:
    among ``CalendarOwnership`` rows for this calendar+org with non-NULL
    ``membership_user_id`` —

    - prefer the ``is_default=True`` owner,
    - else the lone owner (only when there is exactly one),
    - else NULL (multiple owners, no default → skip the membership layer).

    Encoded as ``COALESCE(default_owner_uid, lone_owner_uid)``:

    - 0 owners → both NULL → NULL.
    - 1 owner (default or not) → ``lone_owner_uid`` resolves; default may too.
    - multiple with a default → ``default_owner_uid`` resolves, lone is NULL.
    - multiple without a default → both NULL → NULL.

    Correlated to the calendar through ``calendar_id_ref`` / ``org_id_ref`` so the
    refs must point at the calendar row, not at an intervening subquery.
    """
    from calendar_integration.models import CalendarOwnership

    owners = (
        CalendarOwnership.objects.get_queryset()
        .filter(organization_id=org_id_ref, calendar_fk_id=calendar_id_ref)
        .exclude(membership_user_id__isnull=True)
    )
    default_owner_uid = owners.filter(is_default=True).values("membership_user_id")[:1]
    # Lone owner: the single owner's membership_user_id, but only when the total
    # owner count is exactly 1 (HAVING COUNT(*) = 1).
    lone_owner_uid = (
        owners.values("calendar_fk_id")
        .annotate(_cnt=Count("id"), _uid=Max("membership_user_id"))
        .filter(_cnt=1)
        .values("_uid")[:1]
    )
    return Coalesce(
        Subquery(default_owner_uid, output_field=IntegerField()),
        Subquery(lone_owner_uid, output_field=IntegerField()),
        output_field=IntegerField(),
    )


def _winning_calendar_policy_id_expression(
    calendar_id_ref: OuterRef, org_id_ref: OuterRef, membership_uid_ref: OuterRef
) -> Coalesce:
    """Resolve, in SQL, the id of the BookingPolicy that governs a single calendar.

    Mirrors ``BookingPolicyService.resolve_for_calendar`` precedence **exactly**:

    1. Calendar-level policy (``calendar_fk_id == calendar``).
    2. Owning-membership policy, keyed on the pre-resolved ``membership_uid_ref``
       (see ``_owning_membership_uid_expression``).
    3. Organization-default policy (``is_organization_default``).

    The whole-policy precedence is preserved: the FIRST existing layer wins and
    its id is returned; the caller reads all four fields from *that* policy. No
    per-field COALESCE across layers. Every subquery is org-scoped through
    ``org_id_ref`` so no cross-tenant row can leak in.

    ``membership_uid_ref`` is an expression (typically ``OuterRef`` of a
    pre-computed annotation) that already resolves the owning membership for the
    calendar — passing it in (rather than nesting the owner resolution here)
    keeps every correlation pointed at the calendar row.
    """
    from calendar_integration.models import BookingPolicy

    calendar_policy_id = (
        BookingPolicy.objects.get_queryset()
        .filter(organization_id=org_id_ref, calendar_fk_id=calendar_id_ref)
        .values("id")[:1]
    )
    membership_policy_id = (
        BookingPolicy.objects.get_queryset()
        .filter(organization_id=org_id_ref, membership_user_id=membership_uid_ref)
        .values("id")[:1]
    )
    org_default_policy_id = (
        BookingPolicy.objects.get_queryset()
        .filter(organization_id=org_id_ref, is_organization_default=True)
        .values("id")[:1]
    )
    return Coalesce(
        Subquery(calendar_policy_id, output_field=IntegerField()),
        Subquery(membership_policy_id, output_field=IntegerField()),
        Subquery(org_default_policy_id, output_field=IntegerField()),
        output_field=IntegerField(),
    )


def _policy_field_subqueries(winning_policy_id_ref: OuterRef, org_id_ref: OuterRef) -> dict:
    """Build the four ``effective_*_seconds`` field reads from a winning policy id.

    Reads all four guardrail columns from the single BookingPolicy identified by
    ``winning_policy_id_ref``. When no policy won (NULL id) every read is NULL,
    which ``EffectivePolicy.from_annotation`` decodes as unconstrained. Scoped to
    ``org_id_ref`` for defense-in-depth (the id is already org-resolved).
    """
    from calendar_integration.models import BookingPolicy

    def _field(column: str) -> Subquery:
        return Subquery(
            BookingPolicy.objects.get_queryset()
            .filter(organization_id=org_id_ref, id=winning_policy_id_ref)
            .values(column)[:1],
            output_field=IntegerField(),
        )

    return {
        "effective_lead_time_seconds": _field("lead_time_seconds"),
        "effective_max_horizon_seconds": _field("max_horizon_seconds"),
        "effective_buffer_before_seconds": _field("buffer_before_seconds"),
        "effective_buffer_after_seconds": _field("buffer_after_seconds"),
    }


class CalendarEventQuerySet(BaseOrganizationModelQuerySet, RecurringQuerySetMixin):
    """
    Custom QuerySet for CalendarEvent model to handle specific queries.
    """

    def annotate_recurring_occurrences_on_date_range(
        self, start: datetime.datetime, end: datetime.datetime, max_occurrences=10000, overlap=False
    ):
        """
        Annotated an Array aggregating all occurrences of a recurring event within the specified date range.
        The occurrences are calculated dynamically based on the master event's recurrence rule.
        Each occurrence will be a JSON containing the start_datetime and the end_datetime in UTC.
        """
        return self.annotate(
            recurring_occurrences=GetEventOccurrencesJSON(
                "id", start, end, max_occurrences, overlap=overlap
            )
        )

    def annotate_recurring_occurrences_with_bulk_modifications_on_date_range(
        self, start: datetime.datetime, end: datetime.datetime, max_occurrences=10000
    ):
        """
        Annotate an Array aggregating all occurrences of a recurring event within the specified date range,
        including occurrences from continuation events created by bulk modifications.

        The occurrences are calculated dynamically and include:
        1. Occurrences from the original event (potentially truncated)
        2. Occurrences from any continuation events created by bulk modifications

        Each occurrence will be a JSON containing the start_datetime, end_datetime in UTC,
        and source_event_id to identify which event generated the occurrence.
        """
        return self.annotate(
            recurring_occurrences=GetEventOccurrencesWithBulkModificationsJSON(
                "id", start, end, max_occurrences
            )
        )

    def occurrence_bearing_masters_in_range(
        self, start: datetime.datetime, end: datetime.datetime
    ) -> "CalendarEventQuerySet":
        """Master rows that can yield an occurrence *starting* in ``[start, end)``.

        Deliberately the same shape as the expansion in
        ``CalendarEventService.get_calendar_events_expanded`` — masters only
        (``parent_recurring_object__isnull=True``), one-off events by their own
        ``start_time``, recurring series by their rule — because "what occurrences
        exist in this range" must have exactly one definition in this codebase.

        Two exclusions are load-bearing and are the reason this is a queryset method
        rather than a filter written at the call site:

        - **Recurrence instances and exceptions are excluded here** (they always have
          ``parent_recurring_object`` set). They are not missed: expanding their master
          returns the *exception row itself*, with its own pk and its own moved
          ``start_time``. Enumerating them here as well would produce the identical
          occurrence from two sources.
        - **A bulk-modification continuation is a master in its own right** and *is*
          enumerated, alongside its truncated parent. This is why callers must use
          ``get_occurrences_in_range`` and **not**
          ``get_occurrences_in_range_with_bulk_modifications``: the latter walks
          ``bulk_modifications`` from the parent and would return the continuation's
          occurrences a second time.

          The parent and continuation do **not** reliably tile the timeline. The
          rule *arithmetic* is correct — ``RecurrenceRuleSplitter.split_at_date``
          returns a truncated parent rule (``count=None``, ``until=<last occurrence
          before the split>``) and a continuation rule carrying the remaining count,
          and 1 + 4 == 5 for a five-occurrence series split at its second
          occurrence. The defect is that the truncation never survives to the
          database.

          ``copy.deepcopy`` of a *saved* Django model preserves its ``pk``, so both
          rules the splitter returns are aliases for the original row rather than
          the "new, unsaved instances" ``recurrence_utils`` documents. In
          ``RecurrenceManager.create_bulk_modification_generic`` the parent is
          truncated first and ``continuation_rule.save()`` runs second — and because
          that object still carries the original pk, it issues an ``UPDATE`` against
          the **parent's** rule row, overwriting the ``UNTIL`` just written. The
          continuation itself is unaffected: it is created from an rrule *string*
          and gets a fresh rule row, so the clobber is pure collateral damage.

          The parent therefore keeps generating past the split. Verified persisted
          state, weekly series split at its second occurrence:

          - ``COUNT=5`` series → parent rule left at ``COUNT=4, until=NULL`` (the
            continuation's remaining count), so the parent yields occurrences 1-4
            and the continuation yields 2-5: eight where five exist.
          - **Open-ended series → parent rule left at the original unbounded rule**
            (``count=NULL, until=NULL``). The truncation is erased outright, so the
            parent never stops and the series is duplicated **indefinitely**, not
            merely across the split window. This is the more severe shape.

          Not a metering defect: ``get_calendar_events_expanded`` returns the same
          rows, so the calendar genuinely contains them and the meter is faithful to
          it. Recorded here because this method is where a reader would otherwise
          conclude the two rows cannot overlap. Measured in
          ``payments/tests/test_metering_reconciliation.py``.

        The result is annotated with ``recurring_occurrences`` so a caller iterating it
        and calling ``get_occurrences_in_range`` pays one query for the whole set
        rather than one per master.

        **Cost, measured rather than bounded.** Every recurring master with
        ``until IS NULL`` and ``start_time < end`` is re-selected and re-expanded on
        every sweep — every 15 minutes, forever. Two things about that were checked
        rather than assumed:

        - Expansion is **not** proportional to the series' age.
          ``calculate_recurring_events`` fast-forwards to ``p_start_date``
          arithmetically per frequency (see its ``IF v_range_start >
          v_event.start_time`` branch) instead of stepping from ``start_time``, so a
          series created in 2019 costs the same to expand over a 6-hour window as
          one created yesterday.
        - The cost that *does* grow is the **row count**: this is O(open-ended
          masters in the pooled subtree), and that set only ever grows. It includes
          series that are long finished — a ``COUNT``-bounded rule leaves ``until``
          NULL, so a weekly standup that ended in 2019 is still selected and still
          expanded (to zero occurrences) on every sweep.

        No cheap lower bound is available in the predicate itself: deciding whether a
        rule can still yield an occurrence after ``start`` requires the rule
        arithmetic that lives in the SQL function, not something expressible as an
        indexable ``WHERE``. Materializing a ``last_occurrence_at`` column on
        ``RecurrenceRule`` at write time is the real fix and is deliberately not done
        here — it is a schema change on a hot calendar path, for a cost that is
        currently one cheap function call per stale master.
        """
        return (
            self.filter(parent_recurring_object__isnull=True)
            .filter(
                Q(
                    recurrence_rule__isnull=True,
                    is_recurring_exception=False,
                    start_time__gte=start,
                    start_time__lt=end,
                )
                | Q(
                    Q(recurrence_rule__until__isnull=True) | Q(recurrence_rule__until__gte=start),
                    recurrence_rule__isnull=False,
                    start_time__lt=end,
                )
            )
            .annotate_recurring_occurrences_on_date_range(start, end)
            .select_related("recurrence_rule")
        )


class CalendarSyncQuerySet(BaseOrganizationModelQuerySet):
    """
    Custom QuerySet for CalendarSync model to handle specific queries.
    """

    def get_not_started_calendar_sync(self, calendar_sync_id: int) -> "CalendarSyncType | None":
        """
        Retrieve a calendar sync that has not started yet.
        :param calendar_sync_id: ID of the calendar sync to retrieve.
        :return: CalendarSync instance if found, otherwise None.
        """
        return self.filter(id=calendar_sync_id, status=CalendarSyncStatus.NOT_STARTED).first()


class BlockedTimeQuerySet(BaseOrganizationModelQuerySet, RecurringQuerySetMixin):
    """
    Custom QuerySet for BlockedTime model to handle specific queries.
    """

    def annotate_recurring_occurrences_on_date_range(
        self, start: datetime.datetime, end: datetime.datetime, max_occurrences=10000, overlap=False
    ):
        """
        Annotated an Array aggregating all occurrences of a recurring blocked time within the specified date range.
        The occurrences are calculated dynamically based on the master blocked time's recurrence rule.
        Each occurrence will be a JSON containing the start_datetime and the end_datetime in UTC.
        """
        return self.annotate(
            recurring_occurrences=GetBlockedTimeOccurrencesJSON(
                "id", start, end, max_occurrences, overlap=overlap
            )
        )

    def annotate_recurring_occurrences_with_bulk_modifications_on_date_range(
        self, start: datetime.datetime, end: datetime.datetime, max_occurrences=10000
    ):
        """
        Annotate an Array aggregating all occurrences of a recurring blocked time within the specified date range,
        including occurrences from continuation blocked times created by bulk modifications.

        Each occurrence will be a JSON containing the start_datetime, end_datetime in UTC,
        and source_blocked_time_id to identify which blocked time generated the occurrence.
        """
        return self.annotate(
            recurring_occurrences_with_bulk_modifications=GetBlockedTimeOccurrencesWithBulkModificationsJSON(
                "id", start, end, max_occurrences
            )
        )


class CalendarGroupQuerySet(BaseOrganizationModelQuerySet):
    """
    Custom QuerySet for CalendarGroup model to handle specific queries.
    """

    def only_groups_bookable_in_ranges(
        self, ranges: Iterable[tuple[datetime.datetime, datetime.datetime]]
    ):
        """
        Returns groups where, for every range, every slot has at least
        `required_count` calendars from its pool available
        (per CalendarQuerySet.only_calendars_available_in_ranges).
        """
        return self._only_groups_bookable_in_ranges(ranges, with_bulk_modifications=False)

    def only_groups_bookable_in_ranges_with_bulk_modifications(
        self, ranges: Iterable[tuple[datetime.datetime, datetime.datetime]]
    ):
        """
        Same as `only_groups_bookable_in_ranges` but expands recurring events
        through their bulk-modification continuation series so split-off
        occurrences count against availability.
        """
        return self._only_groups_bookable_in_ranges(ranges, with_bulk_modifications=True)

    def _only_groups_bookable_in_ranges(
        self,
        ranges: Iterable[tuple[datetime.datetime, datetime.datetime]],
        *,
        with_bulk_modifications: bool,
    ):
        from calendar_integration.models import Calendar, CalendarGroupSlot

        ranges = list(ranges)
        if not ranges:
            return self.none()

        calendar_method = (
            "only_calendars_available_in_ranges_with_bulk_modifications"
            if with_bulk_modifications
            else "only_calendars_available_in_ranges"
        )

        qs = self
        for start_datetime, end_datetime in ranges:
            available_calendar_ids = getattr(
                Calendar.objects.get_queryset().filter(organization_id=OuterRef("organization_id")),
                calendar_method,
            )([(start_datetime, end_datetime)]).values("id")
            unsatisfied_slot = (
                CalendarGroupSlot.objects.get_queryset()
                .filter(group_fk_id=OuterRef("id"))
                .annotate(
                    available_in_slot=Count(
                        "memberships",
                        filter=Q(memberships__calendar_fk_id__in=Subquery(available_calendar_ids)),
                        distinct=True,
                    ),
                )
                .filter(available_in_slot__lt=F("required_count"))
            )

            qs = qs.filter(~Exists(unsatisfied_slot))

        return qs

    def annotate_effective_policy(self) -> "CalendarGroupQuerySet":
        """Annotate the four ``effective_*_seconds`` booking-policy columns.

        Resolves, in a single query, the group precedence chain entirely in SQL:

        1. Explicit group policy (``calendar_group_fk == group``) — read whole.
        2. ``most_restrictive`` aggregate across every distinct participant
           calendar (across all slots of the group), where each participant is
           resolved via the single-calendar chain
           (``_winning_calendar_policy_id_subquery``): ``MAX`` of lead /
           buffer_before / buffer_after, and ``MIN`` over the POSITIVE horizons
           (0/unbounded participants excluded; the group horizon is unbounded
           only when every participant is unbounded).
        3. Unconstrained (all NULL) when neither a group policy nor any
           participant constraint exists.

        Decode with ``EffectivePolicy.from_annotation(row)``. Org-scoped: the
        group policy lookup, the participant traversal, and every per-participant
        calendar subquery all filter by the group's own ``organization_id``.
        """
        from calendar_integration.models import BookingPolicy, CalendarGroupSlotMembership

        org_ref = OuterRef("organization_id")
        group_ref = OuterRef("pk")

        # Layer 1: explicit group-level policy id (whole-policy precedence — when
        # present, ALL four fields are read from it, never mixed with the
        # participant aggregate).
        group_policy_id = Subquery(
            BookingPolicy.objects.get_queryset()
            .filter(organization_id=org_ref, calendar_group_fk_id=group_ref)
            .values("id")[:1],
            output_field=IntegerField(),
        )

        def _group_policy_field(column: str) -> Subquery:
            return Subquery(
                BookingPolicy.objects.get_queryset()
                .filter(
                    organization_id=OuterRef("organization_id"),
                    id=OuterRef("_group_policy_id"),
                )
                .values(column)[:1],
                output_field=IntegerField(),
            )

        # Layer 2: most_restrictive over participant calendars. Each participant's
        # effective field is resolved through the single-calendar chain, then
        # aggregated across the distinct participant calendars of the group.
        def _participant_base():
            # One row per (slot-membership) participant. Each participant's
            # effective policy is resolved through the single-calendar chain,
            # correlated to the membership row's own calendar + org.
            return (
                CalendarGroupSlotMembership.objects.get_queryset()
                .filter(
                    organization_id=OuterRef("organization_id"),
                    slot_fk__group_fk_id=OuterRef("pk"),
                )
                .annotate(
                    _p_owning_uid=_owning_membership_uid_expression(
                        OuterRef("calendar_fk_id"), OuterRef("organization_id")
                    ),
                )
                .annotate(
                    _p_policy_id=_winning_calendar_policy_id_expression(
                        OuterRef("calendar_fk_id"),
                        OuterRef("organization_id"),
                        OuterRef("_p_owning_uid"),
                    ),
                )
            )

        def _participant_field(column: str):
            return Subquery(
                BookingPolicy.objects.get_queryset()
                .filter(
                    organization_id=OuterRef("organization_id"),
                    id=OuterRef("_p_policy_id"),
                )
                .values(column)[:1],
                output_field=IntegerField(),
            )

        def _participant_aggregate(column: str, *, positive_only: bool) -> Subquery:
            participants = _participant_base().annotate(_p_value=_participant_field(column))
            aggregate: Min | Max
            if positive_only:
                participants = participants.filter(_p_value__gt=0)
                aggregate = Min("_p_value")
            else:
                aggregate = Max("_p_value")
            return Subquery(
                participants.values("organization_id").annotate(_agg=aggregate).values("_agg")[:1],
                output_field=IntegerField(),
            )

        def _effective(column: str, *, positive_only: bool = False):
            # Whole-policy precedence: when a group policy exists every field is
            # read from it; otherwise the most_restrictive participant aggregate
            # applies. ``Value(0)`` provides the unconstrained fallback so the
            # column is never NULL.
            return Case(
                When(
                    _group_policy_id__isnull=False,
                    then=Coalesce(
                        _group_policy_field(column), Value(0), output_field=IntegerField()
                    ),
                ),
                default=Coalesce(
                    _participant_aggregate(column, positive_only=positive_only),
                    Value(0),
                    output_field=IntegerField(),
                ),
                output_field=IntegerField(),
            )

        qs = self.annotate(_group_policy_id=group_policy_id)
        return qs.annotate(
            effective_lead_time_seconds=_effective("lead_time_seconds"),
            effective_max_horizon_seconds=_effective("max_horizon_seconds", positive_only=True),
            effective_buffer_before_seconds=_effective("buffer_before_seconds"),
            effective_buffer_after_seconds=_effective("buffer_after_seconds"),
        )


class CalendarGroupSlotQuerySet(BaseOrganizationModelQuerySet):
    """
    Custom QuerySet for CalendarGroupSlot model to handle specific queries.
    """


class CalendarGroupSlotMembershipQuerySet(BaseOrganizationModelQuerySet):
    """
    Custom QuerySet for CalendarGroupSlotMembership model to handle specific queries.
    """


class CalendarEventGroupSelectionQuerySet(BaseOrganizationModelQuerySet):
    """
    Custom QuerySet for CalendarEventGroupSelection model to handle specific queries.
    """


class AvailableTimeQuerySet(BaseOrganizationModelQuerySet, RecurringQuerySetMixin):
    """
    Custom QuerySet for AvailableTime model to handle specific queries.
    """

    def only_user_authored(self) -> "AvailableTimeQuerySet":
        """Exclude rows the recurrence machinery derived from another row.

        One availability window the user created can end up as several
        ``AvailableTime`` rows, because editing a recurring series is implemented
        by *inserting* rows rather than mutating occurrences in place:

        * ``AvailabilityService.create_recurring_available_time_exception`` inserts a
          standalone row for a modified occurrence and links it back through
          ``AvailableTimeRecurrenceException.modified_available_time`` (reverse
          accessor ``exception_for``), also flagging it ``is_recurring_exception``.
        * ``create_recurring_bulk_modification`` inserts a continuation row for the
          remainder of a split series, linked by ``bulk_modification_parent``.

        Counting those as separate windows over-reports usage — an organization
        that created three recurring windows and edited three occurrences would
        read as six. Every caller that wants "how many availability windows does
        this organization have" (the billing usage counter above all) wants this
        queryset, not a bare ``filter(...)``.

        Known gap: editing **or cancelling** the first occurrence of a series
        truncates the master row and creates a fresh series row with no link back to
        it (``recurrence_manager.create_recurring_exception_generic``, the
        ``exception_date == parent.start_time.date()`` branch — which never reads
        ``is_cancelled``, so both operations take the identical path). That second
        row is indistinguishable from a genuinely new window in the current schema,
        so it is still counted, and it **compounds**: every subsequent
        first-occurrence edit or cancel on the resulting series adds another
        unlinked row. The over-count is therefore once per operation and unbounded,
        not "one". Closing it needs a new column, not a filter.
        """
        return self.filter(
            exception_for__isnull=True,
            bulk_modification_parent__isnull=True,
            is_recurring_exception=False,
        )

    def count_counted_windows_in_calendar(self, calendar_id: int, ids: Iterable[int]) -> int:
        """How many of ``ids`` are windows the ``availability_windows`` counter counts.

        A batch that deletes rows may offset its own creates against the ceiling, but
        only for rows the usage counter actually counts -- crediting the deletion of a
        derived row (a recurrence exception, a split continuation) that
        :meth:`only_user_authored` excludes would hand out capacity for freeing
        something that was never occupying any, letting a batch grow real usage past
        the ceiling. Expressed here, against ``only_user_authored`` itself, so the
        credit predicate and the counter predicate cannot drift apart.
        """
        return (
            self.only_user_authored().filter(calendar_fk_id=calendar_id, id__in=list(ids)).count()
        )

    def annotate_recurring_occurrences_on_date_range(
        self, start: datetime.datetime, end: datetime.datetime, max_occurrences=10000, overlap=False
    ):
        """
        Annotated an Array aggregating all occurrences of a recurring available time within the specified date range.
        The occurrences are calculated dynamically based on the master available time's recurrence rule.
        Each occurrence will be a JSON containing the start_datetime and the end_datetime in UTC.
        """
        return self.annotate(
            recurring_occurrences=GetAvailableTimeOccurrencesJSON(
                "id", start, end, max_occurrences, overlap=overlap
            )
        )

    def annotate_recurring_occurrences_with_bulk_modifications_on_date_range(
        self, start: datetime.datetime, end: datetime.datetime, max_occurrences=10000
    ):
        """
        Annotate an Array aggregating all occurrences of a recurring available time within the specified date range,
        including occurrences from continuation available times created by bulk modifications.

        Each occurrence will be a JSON containing the start_datetime, end_datetime in UTC,
        and source_available_time_id to identify which available time generated the occurrence.
        """
        return self.annotate(
            recurring_occurrences_with_bulk_modifications=GetAvailableTimeOccurrencesWithBulkModificationsJSON(
                "id", start, end, max_occurrences
            )
        )


class ExternalEventChangeRequestQuerySet(BaseOrganizationModelQuerySet):
    """QuerySet for ExternalEventChangeRequest."""

    def pending(self) -> "ExternalEventChangeRequestQuerySet":
        """Return only PENDING requests."""
        return self.filter(status=ExternalEventChangeRequestStatus.PENDING)

    def for_event(self, event: "CalendarEventType") -> "ExternalEventChangeRequestQuerySet":
        """Return requests targeting a specific CalendarEvent instance.

        Filters through the ``event`` ForeignObject so the organization scope is
        automatically included in the join condition.
        """
        return self.filter(event=event)

    def resolvable_by(
        self, membership: "OrganizationMembershipType"
    ) -> "ExternalEventChangeRequestQuerySet":
        """Return requests the given membership is eligible to resolve.

        Eligibility rules (mirroring ``ExternalEventChangeRequestService.can_resolve``):

        - **Admin** (``membership.is_admin``): sees all change requests in the
          organization.
        - **Member-attendee**: sees only requests whose target event has an
          ``EventAttendance`` row for this membership (matched by ``membership_user_id``
          so the ForeignObject join is honoured).

        The result is always scoped to the membership's organization — the base
        manager enforces ``filter_by_organization`` before this queryset is built.

        Args:
            membership: The ``OrganizationMembership`` whose eligibility to
                evaluate.

        Returns:
            A filtered ``ExternalEventChangeRequestQuerySet`` containing only the
            change requests the membership can resolve.
        """
        from calendar_integration.models import EventAttendance  # noqa: PLC0415

        if membership.is_admin:
            return self

        # Non-admins: restrict to requests whose event they attend.
        attendee_event_ids = EventAttendance.objects.filter(
            organization_id=membership.organization_id,
            membership_user_id=membership.user_id,
            membership__is_active=True,
        ).values("event_fk_id")

        return self.filter(event_fk_id__in=attendee_event_ids)


class BookingPolicyQuerySet(BaseOrganizationModelQuerySet):
    """QuerySet for :class:`~calendar_integration.models.BookingPolicy`.

    A ``BookingPolicy`` is attached to exactly one target: a calendar, an owning
    membership, a calendar group, or the organization default. These chainable
    helpers expose the per-target lookups the resolver uses, all scoped through
    the inherited organization filter.
    """

    def for_calendar(self, calendar_id: int) -> "BookingPolicyQuerySet":
        """Narrow the queryset to the policy attached directly to ``calendar_id``."""
        return self.filter(calendar_fk_id=calendar_id)

    def for_membership(self, membership_user_id: int) -> "BookingPolicyQuerySet":
        """Narrow the queryset to the policy attached to the membership ``membership_user_id``."""
        return self.filter(membership_user_id=membership_user_id)

    def for_calendar_group(self, calendar_group_id: int) -> "BookingPolicyQuerySet":
        """Narrow the queryset to the policy attached to ``calendar_group_id``."""
        return self.filter(calendar_group_fk_id=calendar_group_id)

    def org_default(self) -> "BookingPolicyQuerySet":
        """Narrow the queryset to the organization-default policy."""
        return self.filter(is_organization_default=True)
