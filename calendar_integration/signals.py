"""Reprojects a slot's roster the moment a ``CalendarPool``'s own roster changes.

``CalendarGroupService._reconcile_slot_pools`` is the write path when an admin
attaches or detaches a *pool* from a *slot*. It does not run when a pool's own
roster changes without anyone touching a slot -- e.g. an org admin editing
``CalendarPoolMembershipInline`` on ``CalendarPoolAdmin``, a `manage.py shell`
edit, or a future data migration. Left alone, that gap lets
``CalendarGroupSlotMembership`` drift from the pools it is derived from: a
calendar dropped from a pool stays bookable through every slot that pool is
attached to, and a calendar added to a pool stays silently unbookable. Per the
Calendar Pools plan's Drift mitigation decision, every write that can change a
resolved roster has to go through reconciliation -- these receivers are what
makes that true for edits to ``CalendarPoolMembership`` itself, not only for
edits to the slot <-> pool attachment.

Both receivers reproject every slot the affected pool is attached to, inside
the same transaction as the roster edit -- never ``transaction.on_commit`` --
because a booking read can follow the edit immediately, before any commit
hook would run. They reuse ``CalendarGroupService._reconcile_slot_pools``
rather than duplicating its diff/upsert logic; that method already recomputes
a slot's desired projected rows from the pools' current rosters and is
idempotent, so calling it with the slot's *unchanged* set of attached pools
is exactly "reproject this slot," with no separate code path required.

Bulk-safety:

- A single-row write (``.save()`` / ``.create()`` / ``.delete()`` on one
  instance -- what the admin inline and ``factories.create_calendar_pool_membership``
  do today) is handled directly by the per-row receivers below.
- ``QuerySet.delete()`` fires ``post_delete`` once per row (Django's deletion
  collector always signals per instance), which would reconcile the same
  slot once per deleted row instead of once for the whole operation.
  ``CalendarPoolMembershipQuerySet.delete()`` (see ``querysets.py``) captures
  the distinct pools a bulk delete touches, suppresses these per-row
  receivers for the duration of the delete via ``suppress_pool_membership_reconcile``,
  and reconciles each pool exactly once afterwards.
- ``bulk_create()`` fires no signal at all, for any model. Nothing in this
  codebase calls ``CalendarPoolMembership.objects.bulk_create`` today (the
  only multi-row writer, ``factories.create_calendar_pool``, loops
  ``.create()`` per calendar) -- a caller that starts to must call
  ``reconcile_pools`` explicitly afterwards, in the same transaction.
- ``reconcile_calendar_pool_projections --fix`` (the management command) never
  writes to ``CalendarPoolMembership`` -- it repairs ``CalendarGroupSlotMembership``
  directly, which is the projection these receivers maintain, not its source.
  Its bulk insert/delete therefore cannot recurse into these receivers; see
  the comment at its own write site.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from django.db.models import Model
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver


if TYPE_CHECKING:
    from calendar_integration.models import CalendarPoolMembership


# Set for the duration of a bulk write on CalendarPoolMembership (currently
# only CalendarPoolMembershipQuerySet.delete()) so the per-row receivers below
# skip their own reconcile -- the bulk caller reconciles each affected pool
# exactly once after the whole operation completes instead of once per row.
_suppress_pool_membership_signal: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "calendar_integration_suppress_pool_membership_signal", default=False
)


@contextlib.contextmanager
def suppress_pool_membership_reconcile():
    """Suppress the per-row ``CalendarPoolMembership`` reconcile for this block.

    Used by bulk writers that reconcile the affected pools themselves once
    the whole operation is done, instead of once per row.
    """
    token = _suppress_pool_membership_signal.set(True)
    try:
        yield
    finally:
        _suppress_pool_membership_signal.reset(token)


def reconcile_pools(pool_ids: Iterable[int], organization_id: int) -> None:
    """Reproject every slot attached to any of ``pool_ids``, once per slot.

    Resolves the affected slots explicitly by organization id rather than
    through the bound ``contextvars`` organization context, since this can
    run from paths that never bind one -- the admin, ``manage.py shell``, a
    data migration.

    Worst case per call: one query to resolve the affected slot ids, plus one
    ``CalendarGroupService._reconcile_slot_pools`` call per distinct slot the
    changed pool is attached to (N slots -> N reconciles, never more).
    """
    from calendar_integration.models import CalendarGroupSlot, CalendarGroupSlotPool
    from di_core.containers import container
    from organizations.models import Organization

    pool_id_set = set(pool_ids)
    if not pool_id_set:
        return

    slot_ids = list(
        CalendarGroupSlotPool.objects.filter_by_organization(organization_id)
        .filter(pool_fk_id__in=pool_id_set)
        .values_list("slot_fk_id", flat=True)
        .distinct()
    )
    if not slot_ids:
        return

    if container is None:
        raise RuntimeError(
            "DI container is not wired; the calendar-pool reprojection signal "
            "cannot resolve calendar_group_service before "
            "di_core.apps.DICoreConfig.ready() runs."
        )

    organization = Organization.objects.get(id=organization_id)
    service = container.calendar_group_service()
    service.initialize(organization)

    slots = CalendarGroupSlot.objects.filter_by_organization(organization_id).filter(
        id__in=slot_ids
    )
    for slot in slots:
        # The pool's *attachment* to the slot did not change -- only its
        # roster did -- so the desired end state passed to
        # `_reconcile_slot_pools` is simply "whatever is attached today."
        attached_pool_ids = list(
            CalendarGroupSlotPool.objects.filter_by_organization(organization_id)
            .filter(slot_fk_id=slot.id)
            .values_list("pool_fk_id", flat=True)
        )
        service._reconcile_slot_pools(slot, attached_pool_ids)  # noqa: SLF001


@receiver(
    post_save,
    sender="calendar_integration.CalendarPoolMembership",
    dispatch_uid="calendar_integration.signals.reconcile_pool_on_membership_save",
)
def reconcile_pool_on_membership_save(
    sender: type[Model], instance: "CalendarPoolMembership", **kwargs: Any
) -> None:
    """Reproject the affected pool's slots after a roster row is added."""
    if _suppress_pool_membership_signal.get():
        return
    reconcile_pools({instance.pool_fk_id}, instance.organization_id)


@receiver(
    post_delete,
    sender="calendar_integration.CalendarPoolMembership",
    dispatch_uid="calendar_integration.signals.reconcile_pool_on_membership_delete",
)
def reconcile_pool_on_membership_delete(
    sender: type[Model], instance: "CalendarPoolMembership", **kwargs: Any
) -> None:
    """Reproject the affected pool's slots after a roster row is removed."""
    if _suppress_pool_membership_signal.get():
        return
    reconcile_pools({instance.pool_fk_id}, instance.organization_id)
