"""Stateless recurrence engine shared by events, blocked times, and available times.

``RecurrenceManager`` owns the two generic template-method engines that drive
single-occurrence exceptions and from-a-date bulk modifications of recurring
objects. The logic is identical for every recurring entity family
(``CalendarEvent``, ``BlockedTime``, ``AvailableTime``); only the persistence /
adapter side effects differ, and those are supplied by the caller as callbacks.

Statelessness contract
-----------------------
The manager holds no authentication or per-request state. Everything an engine
needs arrives as a method parameter:

- ``context`` (:class:`CalendarServiceContext`): the immutable auth snapshot the
  facade built on ``authenticate()`` / ``initialize_without_provider()``. The
  engines only need it to run the ``organization`` guard
  (``is_initialized_or_authenticated_calendar_service``) — exactly the guard the
  former ``CalendarService`` methods ran against ``self``. The context exposes the
  same ``organization`` attribute the guard inspects, so behavior is unchanged.
- the ``parent_object``, dates, and ``modification_data`` describing the change.
- the type-specific callbacks (create-new-recurring / create-modified /
  exception-manager update + delete for exceptions; truncate-parent /
  create-continuation / record for bulk modifications). The callbacks are plain
  callables — typically closures defined by the facade that capture the facade so
  they can call back into ``create_event`` / ``create_blocked_time`` / etc.

Because it is stateless, a single ``RecurrenceManager()`` is constructed once on
the facade and reused; it is *not* a DI-container provider.
"""

from __future__ import annotations

import copy
import datetime
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from django.db import models, transaction

from calendar_integration.models import RecurrenceRule, RecurringMixin
from calendar_integration.recurrence_utils import OccurrenceValidator, RecurrenceRuleSplitter
from calendar_integration.services.type_guards import (
    is_initialized_or_authenticated_calendar_service,
)


if TYPE_CHECKING:
    from calendar_integration.services.calendar_service_context import CalendarServiceContext
    from calendar_integration.services.protocols.base_calendar_service import BaseCalendarService


class RecurrenceManager:
    """Stateless engine for recurring-object exceptions and bulk modifications."""

    def create_recurring_exception_generic(
        self,
        context: CalendarServiceContext | None,
        object_type_name: str,
        parent_object: RecurringMixin,
        exception_date: datetime.date,
        is_cancelled: bool,
        modification_data: dict[str, Any] | None = None,
        create_new_recurring_callback: Callable[
            [RecurringMixin, RecurringMixin, RecurrenceRule], RecurringMixin
        ]
        | None = None,
        create_modified_object_callback: Callable[
            [RecurringMixin, datetime.datetime, dict[str, Any]], RecurringMixin
        ]
        | None = None,
        exception_manager_update_callback: Callable[[RecurringMixin, RecurringMixin], None]
        | None = None,
        exception_manager_delete_callback: Callable[[RecurringMixin], None] | None = None,
    ) -> RecurringMixin | None:
        """
        Generic method for creating exceptions for recurring objects (events, blocked times, available times).

        :param context: The shared auth context (used only for the organization guard).
        :param parent_object: The recurring object to create an exception for
        :param exception_date: The datetime of the occurrence to modify/cancel
        :param is_cancelled: True if cancelling the occurrence, False if modifying
        :param modification_data: Dictionary of fields to modify (if not cancelled)
        :param create_new_recurring_callback: Callback to create new recurring object after master modification
        :param create_modified_object_callback: Callback to create modified object for non-cancelled exceptions
        :param exception_manager_update_callback: Callback to update exception manager references
        :param exception_manager_delete_callback: Callback to delete exception manager references
        :return: Created/modified object or None if cancelled
        """
        if not is_initialized_or_authenticated_calendar_service(
            cast("BaseCalendarService", context)
        ):
            raise

        if not parent_object.is_recurring:
            raise ValueError(f"Cannot create exception for non-recurring {object_type_name}")

        # Coerce defensively: `datetime.datetime` *subclasses* `datetime.date`, so
        # the `exception_date: datetime.date` annotation does not stop a caller
        # passing a datetime and mypy will not flag it. Left uncoerced, the
        # comparison below silently never matches (a datetime never equals a date),
        # so an exception on the first occurrence quietly takes the
        # future-occurrence branch and produces a different shape of edit than the
        # caller asked for. `combine` already accepts either, so only the comparison
        # was ever at risk.
        if isinstance(exception_date, datetime.datetime):
            exception_date = exception_date.date()

        exception_datetime = datetime.datetime.combine(
            exception_date, parent_object.start_time.time(), tzinfo=parent_object.start_time.tzinfo
        )

        if exception_date == parent_object.start_time.date():
            # Exception is on the master object date
            second_occurrence = parent_object.get_next_occurrence(exception_datetime)
            old_recurrence_rule = parent_object.recurrence_rule

            if not second_occurrence:
                # No future occurrences, make the object non-recurring
                if exception_manager_delete_callback:
                    exception_manager_delete_callback(parent_object)
                if old_recurrence_rule:
                    old_recurrence_rule.delete()
            else:
                # Create new recurring object starting from second occurrence
                new_recurrence_rule: RecurrenceRule = copy.copy(old_recurrence_rule)
                new_recurrence_rule.id = None
                new_recurrence_rule.count = (
                    new_recurrence_rule.count - 1 if new_recurrence_rule.count else None
                )

                if create_new_recurring_callback:
                    new_recurring_object = create_new_recurring_callback(
                        parent_object, second_occurrence, new_recurrence_rule
                    )
                    if exception_manager_update_callback:
                        exception_manager_update_callback(parent_object, new_recurring_object)

                if old_recurrence_rule:
                    old_recurrence_rule.delete()

            # Update the master object to be non-recurring
            parent_object.recurrence_rule_fk_id = None
            if modification_data:
                for field, value in modification_data.items():
                    if value is not None:
                        setattr(parent_object, field, value)
                    # Keep original value if modification is None (fallback behavior)
            parent_object.save()

            # NOTE: adapter sync intentionally omitted here. Bulk modifications
            # will perform explicit adapter calls when truncating the master series.

            # Return the updated master object
            parent_model = cast(models.Model, parent_object)
            return parent_object.__class__.objects.get(
                organization_id=parent_object.organization_id, id=parent_model.pk
            )

        # Exception is on a future occurrence
        if is_cancelled:
            parent_object.create_exception(exception_datetime, is_cancelled=True)
            return None
        else:
            # Create modified object for the specific occurrence
            if create_modified_object_callback:
                modified_object = create_modified_object_callback(
                    parent_object, exception_datetime, modification_data or {}
                )
                modified_object.is_recurring_exception = True
                modified_object.save()

                parent_object.create_exception(
                    exception_datetime, is_cancelled=False, modified_object=modified_object
                )
                return modified_object
            return None

    def create_recurring_bulk_modification_generic(
        self,
        context: CalendarServiceContext | None,
        object_type_name: str,
        parent_object: RecurringMixin,
        modification_start_date: datetime.datetime,
        is_bulk_cancelled: bool = False,
        modification_data: dict[str, Any] | None = None,
        truncate_parent_callback: Callable[[RecurringMixin, RecurrenceRule | None], RecurringMixin]
        | None = None,
        create_continuation_callback: Callable[
            [RecurringMixin, datetime.datetime, RecurrenceRule | None, dict[str, Any]],
            RecurringMixin,
        ]
        | None = None,
        bulk_modification_record_callback: Callable[
            [RecurringMixin, datetime.datetime, RecurringMixin | None, bool], None
        ]
        | None = None,
        modification_rrule_string: str | None = None,
    ) -> RecurringMixin | None:
        """
        Generic method to apply a bulk modification (from modification_start_date onwards)
        to a recurring series.

        Behaviour:
        1. Validate parent is recurring and modification_start_date is an occurrence.
        2. Compute truncated rule for the original (UNTIL set to previous occurrence).
        3. Compute continuation rule for occurrences from modification_start_date onwards.
        4. Persist continuation object (unless cancelled) using provided callback.
        5. Record a bulk modification record using provided callback.
        Returns the continuation object or None if cancelled.
        """
        if not is_initialized_or_authenticated_calendar_service(
            cast("BaseCalendarService", context)
        ):
            raise

        if not parent_object.is_recurring:
            raise ValueError(
                f"Cannot create bulk modification for non-recurring {object_type_name}"
            )

        # Normalize tz for modification_start_date similar to exceptions
        if modification_start_date.tzinfo is None:
            modification_start_date = datetime.datetime.combine(
                modification_start_date.date(),
                parent_object.start_time.time(),
                tzinfo=parent_object.start_time.tzinfo,
            )

        # Use RecurrenceRule splitting utilities from recurrence_utils
        # Ensure the modification date corresponds to an occurrence
        if not OccurrenceValidator.validate_modification_date(
            parent_object, modification_start_date
        ):
            raise ValueError(
                "Modification start date is not a valid occurrence of the recurring series"
            )

        # Split the rule into truncated and continuation parts
        original_start = parent_object.start_time
        truncated_rule, continuation_rule = RecurrenceRuleSplitter.split_at_date(
            parent_object.recurrence_rule, modification_start_date, original_start
        )

        # Persist changes inside a transaction
        with transaction.atomic():
            # Update original's recurrence_rule to truncated (or remove recurrence_rule if None)
            if truncate_parent_callback:
                parent_object = truncate_parent_callback(parent_object, truncated_rule)

            continuation_obj: RecurringMixin | None = None
            if not is_bulk_cancelled and (continuation_rule or modification_rrule_string):
                # Create continuation recurrence rule and object via callback
                # If caller provided an explicit recurrence string for the continuation,
                # parse it and use that instead of the splitter-generated continuation_rule.
                if modification_rrule_string:
                    continuation_rule = RecurrenceRule.from_rrule_string(
                        modification_rrule_string, parent_object.organization
                    )
                if continuation_rule:
                    continuation_rule.organization = parent_object.organization
                    continuation_rule.save()

                if create_continuation_callback is None:
                    raise ValueError("create_continuation_callback is required when not cancelling")

                continuation_obj = create_continuation_callback(
                    parent_object,
                    modification_start_date,
                    continuation_rule,
                    modification_data or {},
                )

                # Link continuation to parent via bulk_modification_parent field if present
                # Link continuation to parent via bulk_modification_parent field if present
                if hasattr(continuation_obj, "bulk_modification_parent_fk"):
                    continuation_obj.bulk_modification_parent_fk = parent_object
                    continuation_obj.save()

            # Record bulk modification via provided callback (e.g., create EventBulkModification)
            if bulk_modification_record_callback:
                bulk_modification_record_callback(
                    parent_object, modification_start_date, continuation_obj, is_bulk_cancelled
                )
            return continuation_obj
