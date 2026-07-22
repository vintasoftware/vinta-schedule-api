from typing import TYPE_CHECKING

from payments.billing_constants import Entitlement, LimitedResource, LimitRemedy


if TYPE_CHECKING:
    from payments.services.billing_dataclasses import LimitCheckResult


class BillingError(Exception):
    """Root of everything the ``payments`` app raises.

    Deliberately **not** a ``ValueError``. Large parts of the codebase wrap service
    calls in ``except ValueError as e: raise GraphQLError(str(e))`` (e.g.
    ``public_api/mutations.py``) or the REST equivalent, which flattens an
    exception down to its message. Any payments error that must survive that
    journey with its structured fields intact — ``OverLimitError`` above all —
    hangs off this class and *only* this class.

    ``PaymentError`` keeps the ``ValueError`` lineage for backwards compatibility
    with those existing handlers; new structured errors should not.
    """


class PaymentError(BillingError, ValueError):
    """Payment-gateway and billing-data errors.

    Still a ``ValueError`` because callers across the codebase have caught it that
    way since before this tree existed; narrowing that is out of scope here.
    """


class PaymentAdapterError(PaymentError):
    pass


class SubscriptionExternalIdMissingInNotificationError(PaymentAdapterError):
    pass


class ProviderWebhookEventIdMissingError(PaymentAdapterError):
    """The webhook payload has no id usable as the idempotency ledger key.

    Raised by ``BasePaymentAdapter.get_event_id`` / ``BaseSubscriptionAdapter.get_event_id``
    when the provider's own notification id (normally ``get_update_id``'s return value) is
    absent. Without a stable id, delivery cannot be deduplicated safely.
    """

    def __init__(self, message="Webhook payload is missing the notification id"):
        super().__init__(message)


class UnknownPaymentProviderError(PaymentError):
    """Raised when a ``provider`` slug doesn't match any registered adapter.

    Surfaces as a 404 at the webhook views — an unregistered provider slug in the
    URL is a routing/configuration error, not an authentication failure.
    """

    def __init__(self, provider: str):
        super().__init__(f"Unknown payment provider: {provider!r}")
        self.provider = provider


class MissingBillingProfileError(PaymentError):
    def __init__(self, message="User does not have a billing profile"):
        super().__init__(message)


class BillingProfileContactEmailMissingError(PaymentError):
    def __init__(
        self,
        message="BillingProfile.contact_email is required to send the payer identity "
        "to the payment gateway",
    ):
        super().__init__(message)


class BillingRootCycleError(PaymentError):
    """Raised by ``resolve_billing_root`` when the ``parent`` chain starting from
    an organization revisits an organization it already walked through.

    ``parent`` is user-mutable data (Django admin), so a cycle is reachable in
    practice. Returning an arbitrary node from the cycle (as opposed to raising)
    would silently leave every organization on the cycle without a resolvable
    billing root, which ``get_effective_limit`` would then be handed.
    """

    def __init__(self, organization_id: int, visited_ids: set[int]):
        super().__init__(
            f"Cycle detected while resolving the billing root for organization "
            f"{organization_id}: revisited organization ids {sorted(visited_ids)}"
        )
        self.organization_id = organization_id
        self.visited_ids = visited_ids


class MissingSeedBillingPlanError(PaymentError):
    """Raised when a ``BillingPlan`` slug a migration/backfill depends on (e.g. the
    ``unlimited`` plan seeded by ``payments.0007``) is missing at runtime.

    A missing seeded plan means a corrupted or out-of-order deploy — this must
    fail loudly rather than silently leave every organization plan-less with no
    signal and no re-run path.
    """

    def __init__(self, slug: str):
        super().__init__(
            f"Required seed BillingPlan {slug!r} is missing. Check migration order "
            "and seed data before re-running."
        )
        self.slug = slug


class InapplicableInvitationExclusionError(BillingError):
    """Raised when ``exclude_invitation_id`` is passed for a resource whose usage
    counter does not read it — i.e. anything but ``organization_members``.

    A programming error, not a runtime condition: the caller believes a pending
    invitation was excluded from the count and it was not, so the number they get
    back is wrong in a direction nothing else will contradict.
    """

    def __init__(self, resource_key: str):
        super().__init__(
            f"exclude_invitation_id is only meaningful for "
            f"{LimitedResource.ORGANIZATION_MEMBERS!r}, not {resource_key!r}: no other usage "
            "counter reads it, so it would be silently ignored."
        )
        self.resource_key = resource_key


class IncompleteBillingPlanError(BillingError):
    """Raised when a ``BillingPlan`` a subscription is being placed on carries no
    ``PlanLimit`` row for some ``LimitedResource`` member.

    The catalog never expresses "this plan does not include X" by omission — it
    expresses it with an explicit row (``limit_value=0``), exactly as the seeded
    ``free`` plan does for ``public_api_system_users``. Omission is therefore a
    catalog *authoring* error, and there is no safe way to interpret it at
    runtime: an absent (or stale ``limit_value=None``) row reads as **unlimited**
    in ``EntitlementService``, so letting the plan change through would hand the
    omitted resource an infinite ceiling on a downgrade; materializing it as
    ``limit_value=0`` would instead block an organization on a resource nobody
    agreed to restrict, which the rollout forbids.

    So the plan change is refused and the gap is surfaced to whoever authored it.
    ``BillingPlan.clean`` and ``BillingPlanAdmin``'s limit inline raise the same
    condition as a ``ValidationError`` at authoring time, which is where a support
    admin can actually act on it; this exception is the runtime backstop for a plan
    that reached the database some other way.

    Inherits ``BillingError`` rather than ``PaymentError`` (a ``ValueError``) so
    the ``except ValueError`` wrappers around service calls cannot flatten a
    catalog-integrity failure into a user-facing validation message.
    """

    def __init__(self, plan_slug: str, missing_resource_keys: list[str]):
        super().__init__(
            f"BillingPlan {plan_slug!r} carries no PlanLimit row for "
            f"{missing_resource_keys}. Every plan must carry a row for every "
            "LimitedResource member — 'not included' is limit_value=0, never omission, "
            "because an omitted row reads as unlimited."
        )
        self.plan_slug = plan_slug
        self.missing_resource_keys = missing_resource_keys


class BillingPeriodResolutionError(BillingError):
    """Raised when the billing cycle containing a given moment cannot be reached by
    stepping from a ``Subscription``'s current period.

    ``resolve_billing_period`` walks whole intervals backwards or forwards from
    ``current_period_start``. A moment that is still outside the period after an
    implausible number of steps means the subscription's period fields are
    corrupt — most likely ``current_period_end <= current_period_start``, which
    makes the walk unable to advance — and continuing would loop forever.

    Raising is the only safe outcome: the alternative is stamping
    ``billing_period_start`` with a wrong cycle, which bills real usage to the
    wrong invoice and is invisible until a customer disputes it.
    """

    def __init__(self, subscription_id: int, moment: object, steps: int):
        super().__init__(
            f"Could not resolve the billing period containing {moment!r} for "
            f"Subscription {subscription_id} within {steps} interval steps. The "
            "subscription's current_period_start/current_period_end are likely "
            "inconsistent."
        )
        self.subscription_id = subscription_id
        self.moment = moment
        self.steps = steps


class InvalidLimitCheckResultError(BillingError):
    """Raised by ``OverLimitError.from_check_result`` when the ``LimitCheckResult``
    it was given breaks the invariant ``EntitlementService.check_limit`` documents:
    a blocked (``allowed=False``) result must carry non-``None``
    ``current_usage``/``ceiling``/``remedy``, and this must never be called on an
    ``allowed=True`` result at all.

    A broken invariant in ``check_limit`` itself, not a runtime condition a caller
    should ever hit. Inherits ``BillingError`` rather than plain ``ValueError`` —
    ``PaymentError`` is itself a ``ValueError``, and several call sites across the
    codebase wrap service calls in ``except ValueError as e: raise ...(str(e))``,
    which would flatten this invariant violation into a user-facing validation
    message instead of surfacing it as the programming error it is.
    """


class OverLimitError(BillingError):
    """Raised by a guarded service method when creating one more of a resource
    would take the organization past its effective ceiling.

    Inherits ``BillingError`` rather than ``PaymentError`` precisely because
    ``PaymentError`` is a ``ValueError``: ``public_api/mutations.py`` and
    ``calendar_integration/views.py`` both wrap service calls in
    ``except ValueError as e: raise ...(str(e))``, which would downgrade this to a
    message-only error and drop ``code`` / ``resource`` / ``current_usage`` /
    ``limit`` / ``remedy``. Several of those wrapped call sites guard resources
    that are ``LimitedResource`` members (``webhook_subscriptions`` among them), so
    the byte-identical-across-surfaces contract below depends on this base class.

    Carries the four fields of the shared over-limit error contract so
    every surface — DRF (via ``common.exception_handlers.vinta_exception_handler``)
    and the public GraphQL API — renders a byte-identical body:

    .. code-block:: json

        {
          "detail": "Organization is at its limit for organization members.",
          "code": "limit_exceeded",
          "resource": "organization_members",
          "current_usage": 10,
          "limit": 10,
          "remedy": "purchase_add_on"
        }

    Rendered as HTTP 402 Payment Required rather than 403, so a client can tell
    "you may not do this" apart from "you have run out of capacity".
    """

    code = "limit_exceeded"

    def __init__(
        self,
        resource_key: str,
        current_usage: int,
        limit: int,
        remedy: str,
        detail: str | None = None,
    ):
        self.resource_key = resource_key
        self.current_usage = current_usage
        self.limit = limit
        self.remedy = remedy
        self.detail = detail or self.build_detail(resource_key)
        super().__init__(self.detail)

    @staticmethod
    def build_detail(resource_key: str) -> str:
        """Human-readable half of the error body.

        Uses the ``LimitedResource`` label when the key is a known member and falls
        back to the raw key otherwise, so an unrecognized key degrades to a usable
        message instead of raising while building an error.
        """
        try:
            label = LimitedResource(resource_key).label.lower()
        except ValueError:
            label = resource_key
        return f"Organization is at its limit for {label}."

    def as_error_body(self) -> dict:
        """The shared contract body, used by every rendering surface."""
        return {
            "detail": self.detail,
            "code": self.code,
            "resource": self.resource_key,
            "current_usage": self.current_usage,
            "limit": self.limit,
            "remedy": self.remedy,
        }

    @classmethod
    def from_check_result(cls, result: "LimitCheckResult") -> "OverLimitError":
        """Build the error from a blocked ``EntitlementService.check_limit`` result.

        Every guarded creation path ends the same way: call ``check_limit`` and,
        if ``result.allowed`` is ``False``, raise this. Centralizing the
        conversion here means every call site does the identical narrowing
        exactly once, rather than repeating "current_usage/ceiling/remedy are
        only guaranteed non-None on the blocked branch" (see
        ``LimitCheckResult``'s docstring) at each of them.

        :raises InvalidLimitCheckResultError: if called on an ``allowed`` result,
            or on a blocked one missing any of the three fields it is documented
            to carry — both are programming errors (a broken ``check_limit``
            invariant), not a runtime condition a caller should ever hit.
        """
        if result.allowed:
            raise InvalidLimitCheckResultError(
                "from_check_result() called on an allowed LimitCheckResult"
            )
        if result.current_usage is None or result.ceiling is None or result.remedy is None:
            raise InvalidLimitCheckResultError(
                f"Blocked LimitCheckResult for {result.resource_key!r} is missing "
                "current_usage/ceiling/remedy; EntitlementService.check_limit must "
                "populate all three when allowed is False."
            )
        return cls(
            resource_key=result.resource_key,
            current_usage=result.current_usage,
            limit=result.ceiling,
            remedy=result.remedy,
        )

    @classmethod
    def from_restricted_organization(cls) -> "OverLimitError":
        """Build the error for a write attempted while the caller's billing root is
        ``RESTRICTED`` -- an expired grace window with no resolution.

        Not a ``LimitedResource`` at all, so there is no unit to count:
        ``current_usage``/``limit`` are both ``0``, the same "0 of an allowance of
        0" convention ``from_missing_entitlement`` uses for boolean gates.
        ``remedy`` is always ``resolve_billing`` -- the only way out of
        ``RESTRICTED`` is paying (or the org's usage dropping back under free
        limits, which the dunning sweep resolves on its own, not anything the
        caller can do from here). ``resource_key`` is a sentinel, not a
        ``LimitedResource`` member -- ``build_detail`` falls back to the raw key
        for anything it does not recognize.
        """
        return cls(
            resource_key="organization_restricted",
            current_usage=0,
            limit=0,
            remedy=LimitRemedy.RESOLVE_BILLING,
            detail=(
                "Organization is restricted pending resolution of an outstanding billing issue."
            ),
        )

    @classmethod
    def from_missing_entitlement(cls, entitlement_key: str) -> "OverLimitError":
        """Build the error for a denied boolean feature gate (``Entitlement``, not
        ``LimitedResource``).

        An entitlement has no usage count or ceiling to report -- it is granted or
        it is not -- so ``current_usage``/``limit`` are both ``0`` (0 of an
        allowance of 0) rather than ``None``: the shared contract's ``current_usage``
        and ``limit`` fields are typed as ``int`` on every surface that renders this
        error, and every existing renderer (``vinta_exception_handler``,
        ``raise_over_limit_graphql_error``) reads them unconditionally.

        ``remedy`` is always ``upgrade_plan`` -- unlike a pre-paid ceiling, an
        entitlement cannot be lifted by purchasing more of the same resource; only a
        plan that grants it does. This intentionally does not consult billing state
        the way ``EntitlementService._resolve_remedy_for`` does for limits: an
        organization in grace/restricted is already told to resolve billing by
        whichever limit check it hits first on the same request, and duplicating
        that lookup here would mean re-fetching the subscription this call has no
        other reason to need.
        """
        try:
            label = Entitlement(entitlement_key).label.lower()
        except ValueError:
            label = entitlement_key
        return cls(
            resource_key=entitlement_key,
            current_usage=0,
            limit=0,
            remedy=LimitRemedy.UPGRADE_PLAN,
            detail=f"Organization does not have the {label} entitlement.",
        )


class NoDefaultBillingPlanError(PaymentError):
    """Raised when no active ``BillingPlan`` has
    ``is_default_for_new_organizations=True`` — e.g. the default plan was
    deactivated in admin without a replacement being marked default first.
    """

    def __init__(
        self,
        message="No active BillingPlan is marked is_default_for_new_organizations=True",
    ):
        super().__init__(message)


class PaymentTokenRequiredError(PaymentError):
    """Raised when an upgrade needs to attach a payment instrument to the
    provider and the caller supplied no ``payment_token``.

    Only reachable the *first* time a billing root ever pays: once
    ``Subscription.external_id`` is set, later upgrades reuse the instrument
    already on file with the provider and drive it through
    ``BaseSubscriptionAdapter.change_subscription_plan`` instead, which takes no
    token.
    """

    def __init__(self, organization_id: int):
        super().__init__(
            f"Organization {organization_id} has no payment method on file with the "
            "provider yet -- a payment_token is required to attach one before "
            "upgrading to a paid plan."
        )
        self.organization_id = organization_id


class UnconfirmedPlanChangeError(PaymentError):
    """Raised when a new plan change is requested while an earlier upgrade's
    charge is still awaiting confirmation from a subscription-payment webhook.

    Applying a second change now would leave ``Subscription.plan`` pointing at
    the newest requested tier, so the first (already-charging) upgrade's webhook
    would grant *that* tier's capacity rather than the plan its charge paid for.
    The caller must wait for the in-flight change to confirm (or fail) before
    requesting another. Re-requesting the *same* plan/interval is still a no-op
    rather than an error (it never reaches this check).
    """

    def __init__(self, organization_id: int):
        super().__init__(
            f"Organization {organization_id} already has a plan change awaiting "
            "payment confirmation -- wait for it to settle before requesting another."
        )
        self.organization_id = organization_id


class IllegalBillingStateTransitionError(BillingError):
    """Raised by ``payments.services.billing_state_machine.transition_billing_state``
    when the requested ``(from_state, to_state)`` pair is not an allowed edge in
    the billing state machine's transition table.

    The transition table is the authority on which transitions exist -- an
    illegal transition is refused outright, never silently coerced or turned into
    a no-op. Inherits ``BillingError`` rather than
    ``PaymentError`` (a ``ValueError``) so the ``except ValueError`` wrappers
    scattered across the codebase cannot flatten a state-machine violation into a
    generic validation message.
    """

    def __init__(self, subscription_id: int | None, from_state: str, to_state: str):
        super().__init__(
            f"Illegal billing state transition for Subscription {subscription_id}: "
            f"{from_state!r} -> {to_state!r} is not on the billing lifecycle diagram."
        )
        self.subscription_id = subscription_id
        self.from_state = from_state
        self.to_state = to_state


class AddOnNotPurchasableError(PaymentError):
    """Raised when ``purchase_add_on`` is asked to sell capacity for a resource
    whose current ``SubscriptionPlanLimit`` carries no ``overage_unit_price`` —
    there is no catalog-derived price to charge, and inventing one here would be
    exactly the "bespoke pricing" this billing model deliberately does not support.
    """

    def __init__(self, resource_key: str):
        super().__init__(
            f"{resource_key!r} has no overage_unit_price on the subscription's current "
            "plan, so it cannot be purchased as an add-on."
        )
        self.resource_key = resource_key
