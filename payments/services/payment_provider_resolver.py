"""Resolves which payment provider governs an organization's *future* charges.

A sibling of ``payments.services.payment_service`` rather than a method on
``PaymentService`` itself: ``PaymentService`` is generic over the adapter types
(``PaymentAdapter``/``SubscriptionAdapter``/``SubscriptionPlanFactory``) and its
constructor is exactly what Phase 4 of the payment-provider-selection plan rewires
(dropping the singular ``payment_gateway``/``subscription_gateway`` injections). This
resolver has zero dependency on any adapter -- it only reads
``settings.DEFAULT_PAYMENT_PROVIDER`` and ``BillingProfile.payment_provider`` -- so keeping
it out of ``PaymentService`` means Phase 3 (this module's only consumer today) does not
touch, and cannot destabilize, the class Phase 4 is about to refactor.

Single place both the provider-credentials endpoints (Phase 3,
``payments.views.PaymentProviderViewSet``) and the charge-routing refactor (Phase 4,
``PaymentService.create_payment``/``create_subscription``) call to resolve an
organization's provider -- so the pin -> default resolution rule cannot drift between the
read path and the write path.
"""

import logging

from django.conf import settings

from payments.models import BillingProfile
from tenancy.models import Organization


logger = logging.getLogger(__name__)


class PaymentProviderResolver:
    """Resolves the payment provider governing an organization's next charge.

    Stateless -- reads ``settings.DEFAULT_PAYMENT_PROVIDER`` and the organization's own
    ``BillingProfile.payment_provider`` pin. No adapter or secret credential is reachable
    from this class.
    """

    def resolve_for_organization(self, organization: Organization) -> str:
        """The provider governing ``organization``'s next charge: its pin when non-empty,
        ``settings.DEFAULT_PAYMENT_PROVIDER`` otherwise.

        An organization with no ``BillingProfile`` at all resolves to the default, exactly
        like one whose profile exists but carries an empty pin -- see
        ``BillingProfile.payment_provider``'s docstring: ``""`` means both "never paid" and
        "explicitly un-pinned by staff", and both cases resolve identically. Callers must
        not try to distinguish the two.
        """
        try:
            billing_profile = organization.billing_profile
        except BillingProfile.DoesNotExist:
            return self.resolve_default()
        return billing_profile.payment_provider or self.resolve_default()

    def resolve_default(self) -> str:
        """The system-wide default provider."""
        return settings.DEFAULT_PAYMENT_PROVIDER
