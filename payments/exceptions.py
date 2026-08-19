"""Transitional re-export of ``vinta_billing.exceptions`` -- plus the one host
exception the package has no counterpart for.

Twenty-five of the twenty-six exceptions this module used to define moved to the
package unchanged (same class names, same ``code`` discriminators, same rendered
bodies) and are re-exported below.

``InapplicableInvitationExclusionError`` did not, and could not: it guards a
concept -- "a pending invitation currently being accepted" -- that is this
product's, not a billing library's. The package forwards per-call counter data
opaquely (``EntitlementService.check_limit(usage_extra=...)`` →
``UsageContext.extra``) and documents that it never reads it, so it has no place
to raise from. **The guard is therefore not enforced from this migration
onward**; see the phase report and the ``vinta-django-billing`` gap entry. The
class is kept defined so the name survives for whatever the package ships to
restore it.

**Removed in Phase 6** of
``ai-plans/2026-08-19-MIGRATE_BILLING_ENGINE_TO_VINTA_DJANGO_BILLING_IMPLEMENTATION_PLAN.md``.
"""

from vinta_billing.exceptions import (
    AddOnNotPurchasableError,
    BillingError,
    BillingPeriodResolutionError,
    BillingProfileContactEmailMissingError,
    BillingRootCycleError,
    ChargeDeclinedError,
    CollectionNotSupportedError,
    IllegalBillingStateTransitionError,
    IncompleteBillingPlanError,
    InvalidLimitCheckResultError,
    MissingBillingProfileError,
    MissingSeedBillingPlanError,
    NoDefaultBillingPlanError,
    NoOutstandingBalanceError,
    OverLimitError,
    PaymentAdapterError,
    PaymentError,
    PaymentProviderNotConfiguredError,
    PaymentTokenRequiredError,
    ProviderWebhookEventIdMissingError,
    RetryPaymentNotApplicableError,
    SubscriptionExternalIdMissingInNotificationError,
    SubscriptionNotAttachedError,
    UnconfirmedPlanChangeError,
    UnknownPaymentProviderError,
)

from payments.billing_constants import LimitedResource


__all__ = [
    "AddOnNotPurchasableError",
    "BillingError",
    "BillingPeriodResolutionError",
    "BillingProfileContactEmailMissingError",
    "BillingRootCycleError",
    "ChargeDeclinedError",
    "CollectionNotSupportedError",
    "IllegalBillingStateTransitionError",
    "InapplicableInvitationExclusionError",
    "IncompleteBillingPlanError",
    "InvalidLimitCheckResultError",
    "MissingBillingProfileError",
    "MissingSeedBillingPlanError",
    "NoDefaultBillingPlanError",
    "NoOutstandingBalanceError",
    "OverLimitError",
    "PaymentAdapterError",
    "PaymentError",
    "PaymentProviderNotConfiguredError",
    "PaymentTokenRequiredError",
    "ProviderWebhookEventIdMissingError",
    "RetryPaymentNotApplicableError",
    "SubscriptionExternalIdMissingInNotificationError",
    "SubscriptionNotAttachedError",
    "UnconfirmedPlanChangeError",
    "UnknownPaymentProviderError",
]


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
