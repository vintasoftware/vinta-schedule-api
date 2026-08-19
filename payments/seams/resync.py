"""Resumes calendar sync when a billing restriction is lifted.

``RESTRICTED`` is the one billing state that pauses external calendar sync (see
``EntitlementService.is_billing_root_restricted`` and
``calendar_integration``'s sync-pause guard). Leaving it therefore has to
reconcile the drift that accumulated while sync was off, and the host's
``DunningService`` used to do that inline: on any transition out of
``RESTRICTED``, fan a resync task out over the billing root's pooled subtree.

``vinta_billing``'s ``DunningService`` cannot. A billing library has no notion
of a calendar, let alone of a sync that was paused, so it publishes
``vinta_billing.signals.billing_restriction_lifted`` at the same point instead
-- after the state write, inside the caller's transaction -- carrying the
subscription and the pooled organization ids the inline call resolved for
itself. Connecting a receiver here is what keeps the resync happening.

Without this seam the failure is silent and slow: the organization pays, its
writes are unblocked, and its calendars quietly never catch up on anything that
changed at the provider while it was restricted.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from django.db import transaction
from django.db.models import Model
from django.dispatch import receiver

from vinta_billing.signals import billing_restriction_lifted


@receiver(billing_restriction_lifted, dispatch_uid="payments.seams.resync.resume_calendar_sync")
def resume_calendar_sync(
    sender: type[Model],
    subscription: Any,
    organization_ids: Sequence[int],
    **kwargs: Any,
) -> None:
    """Queue a resync of every calendar the pooled subtree owns.

    Fanned out per organization rather than per calendar -- each
    ``resync_organization_calendars_task`` resolves its own organization's
    calendars -- over exactly the id set the engine resolved, which is the same
    set every usage counter and the sync-pause guard itself answer for.

    ``transaction.on_commit`` as belt-and-braces: in 0.4.0,
    ``DunningService._trigger_resync_after_recovery`` already sends
    ``billing_restriction_lifted`` from inside its own ``transaction.on_commit``,
    so by the time this receiver runs the transaction that moved
    ``billing_state`` off ``RESTRICTED`` has already committed, and this call has
    no pending transaction to defer past -- Django runs the callback immediately.
    Kept anyway, and correct either way: if a future package version ever sent
    the signal from inside the still-open transaction, queuing before that
    commits would let a worker pick the resync up and read a subscription that
    is, as far as its own snapshot is concerned, still restricted.
    """
    # Late, and it has to be: importing `calendar_integration.tasks
    # .calendar_sync_tasks` pulls in `calendar_integration.services`, which
    # reaches back into `payments.seams`. At module scope that is a cycle; the
    # host's pre-move `DunningService` deferred the same import for the same
    # reason.
    from calendar_integration.tasks.calendar_sync_tasks import (
        resync_organization_calendars_task,
    )

    ids = list(organization_ids)
    transaction.on_commit(
        lambda: [
            resync_organization_calendars_task.delay(organization_id=organization_id)
            for organization_id in ids
        ]
    )
