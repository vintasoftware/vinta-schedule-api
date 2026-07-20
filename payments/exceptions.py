class PaymentError(ValueError):
    pass


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
    billing root, which Phase 5's ``get_effective_limit`` would then be handed.
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


class OverLimitError(PaymentError):
    """Raised by a guarded service method when creating one more of a resource
    would take the organization past its effective ceiling.

    Carries the four fields of the plan's shared over-limit error contract so
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
        from payments.billing_constants import LimitedResource

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
