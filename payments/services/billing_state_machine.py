"""The single definition of which ``BillingState`` transitions are legal.

Encodes the spec's lifecycle diagram (Billing Plans and Limits spec, Use-case
5's ``stateDiagram-v2``) -- **the diagram is the authority on which edges
exist** -- plus the handful of cancellation edges the product's cancel action
requires beyond the single ``ACTIVE -> CANCELLED`` the diagram draws (each
called out inline in ``LEGAL_BILLING_STATE_TRANSITIONS`` with its own
justification). Every place in the codebase that changes
``Subscription.billing_state`` (``DunningService``,
``SubscriptionService.confirm_plan_change`` / ``cancel_subscription``) goes
through ``transition_billing_state`` rather than writing the field directly, so
the set of transitions the code can actually perform and the set this table
permits are the same set, defined exactly once. This is deliberately a bare
module-level
function with no DI dependency (not a method on a service class): both
``DunningService`` (payment-provider/dunning-driven transitions) and
``SubscriptionService`` (plan-change-driven transitions) need it, and neither
should have to depend on the other through the DI container just to reach a
state-machine check.

The one edge from the diagram this module does **not** encode is
``[*] --> Free`` -- that is subscription *creation*
(``SubscriptionService.create_subscription_for_organization``), not a
transition of an existing row, so it has no ``from_state`` to validate against.
"""

import logging

from payments.billing_constants import BillingState
from payments.exceptions import IllegalBillingStateTransitionError
from payments.models import Subscription


logger = logging.getLogger(__name__)


#: Every edge of the spec's lifecycle diagram, as ``(from_state, to_state)`` pairs.
#: ``ACTIVE -> ACTIVE`` ("renewal succeeds") is the only self-loop the diagram draws
#: explicitly; every *other* state's self-loop is granted separately by
#: ``transition_billing_state``'s same-state short-circuit below (the guiding
#: decision that a transition must be idempotent on its target state -- entering
#: GRACE twice, or a dunning retry firing twice, must not raise).
LEGAL_BILLING_STATE_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        (BillingState.FREE, BillingState.ACTIVE),  # upgrade paid
        (BillingState.ACTIVE, BillingState.ACTIVE),  # renewal succeeds
        # payment fails (driven by DunningService.enter_grace).
        # TODO(phase-11): the diagram's second reason for this edge -- "downgrade
        # leaves org over limit" -- has no driver yet; _schedule_downgrade stamps
        # grace_period_ends_at but leaves billing_state ACTIVE, so process_dunning
        # never sweeps it. Payment-failure is the only reason that reaches it today.
        (BillingState.ACTIVE, BillingState.GRACE),
        # payment fails on a first-upgrade charge (driven by DunningService.enter_grace).
        # TODO(phase-11): the diagram's "downgrade leaves org over limit" reason for
        # this edge has no driver yet -- same gap as (ACTIVE, GRACE) above.
        (BillingState.FREE, BillingState.GRACE),
        (BillingState.GRACE, BillingState.ACTIVE),  # payment succeeds
        (BillingState.GRACE, BillingState.FREE),  # org returns under free limits
        (BillingState.GRACE, BillingState.RESTRICTED),  # grace period expires
        (BillingState.RESTRICTED, BillingState.ACTIVE),  # payment succeeds
        (BillingState.RESTRICTED, BillingState.FREE),  # org returns under free limits
        (BillingState.ACTIVE, BillingState.CANCELLED),  # cancellation
        # Cancellation from the other live states. The spec diagram draws only
        # ACTIVE -> CANCELLED, but the product's cancel action (Phase 9's endpoint,
        # SubscriptionService.cancel_subscription) is offered from any live state,
        # so the machine must be able to perform what the product does:
        (BillingState.FREE, BillingState.CANCELLED),  # cancel a free-tier subscription
        (BillingState.GRACE, BillingState.CANCELLED),  # give up on dunning and cancel
        (BillingState.RESTRICTED, BillingState.CANCELLED),  # cancel instead of paying to recover
        (BillingState.CANCELLED, BillingState.FREE),  # cycle ends (Phase 13 sweep)
    }
)


def is_legal_billing_state_transition(from_state: str, to_state: str) -> bool:
    """Whether ``from_state -> to_state`` is on the diagram (same-state included --
    see ``transition_billing_state``'s docstring for why)."""
    return from_state == to_state or (from_state, to_state) in LEGAL_BILLING_STATE_TRANSITIONS


def transition_billing_state(
    subscription: Subscription, to_state: str
) -> tuple[Subscription, bool]:
    """Validate and, if needed, write ``subscription.billing_state = to_state``.

    Returns ``(subscription, changed)``. ``changed`` is ``False`` when
    ``subscription`` was already on ``to_state`` -- an **idempotent no-op**: no
    write happens, so a caller that stamps additional bookkeeping (e.g.
    ``grace_period_ends_at``) alongside the transition knows not to re-stamp it.
    This is what makes entering GRACE twice, or a dunning retry firing twice
    under ``CELERY_TASK_ACKS_LATE`` redelivery, safe rather than a double-send.

    :raises IllegalBillingStateTransitionError: ``to_state`` is reachable from
        ``subscription``'s current state only through an edge that is not on the
        spec's lifecycle diagram. Rejected outright -- never silently coerced or
        proceeded with -- per the plan's guiding decision. Every caller that can
        legitimately be asked for a transition the diagram does not draw (e.g. a
        stray webhook confirming a charge for an already-``CANCELLED``
        subscription) is responsible for checking first or catching this, not
        this function for loosening the rule on their behalf.
    """
    from_state = subscription.billing_state
    if from_state == to_state:
        return subscription, False
    if (from_state, to_state) not in LEGAL_BILLING_STATE_TRANSITIONS:
        raise IllegalBillingStateTransitionError(subscription.pk, from_state, to_state)
    subscription.billing_state = to_state
    subscription.save(update_fields=["billing_state"])
    logger.info(
        "Subscription %s billing_state transitioned %s -> %s",
        subscription.pk,
        from_state,
        to_state,
    )
    return subscription, True
