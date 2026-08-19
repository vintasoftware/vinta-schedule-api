"""This project's mounting of ``vinta_billing.views``.

The endpoints themselves are the package's from
``payments/migrations/0024_move_billing_to_vinta_billing.py`` onward -- the
~600 lines of view code this module used to hold now live in
``vinta_billing/views.py``, and 0.4.0 made them mountable at all (0.3.0 took
their services as required keyword-only constructor arguments with no default,
so a router resolved the URL and then raised ``TypeError`` on the first
request).

Two things stay here, and both are this project's rather than a billing
library's:

1. **Which organization a request acts on.** ``X-Organization-Id``, with this
   API's own 400 for an ambiguous caller and 403 for a non-member. See
   ``payments.seams.view_scoping``.
2. **Where the services come from.** ``di_core.containers`` builds every
   billing service here, which is the plan's "DI ownership: the host's"
   decision. The package's constructors fall back to
   ``vinta_billing.services.container`` when passed nothing -- a second,
   parallel set of instances built from ``VINTA_BILLING`` that no
   ``di_container.<provider>.override(...)`` can reach. Passing the injected
   service explicitly is what keeps one container in charge, and it is also
   what keeps a provider adapter swapped in a test actually swapped.

Nothing else is restated: no serializer, no queryset, no action body.

**Phase 2** deletes ``payments/routes.py`` and mounts
``vinta_billing.routing.get_routes()`` / ``get_extra_patterns()`` from
``vinta_schedule_api/urls.py``. Those name the package's classes directly, so
they cannot carry either of the two things above: Phase 2 has to keep mounting
these subclasses, or the package needs settings seams for the view mixin and
the service container. Recorded in the phase report.

Every viewset below carries a bare ``__doc__ = <package class>.__doc__``
assignment rather than its own docstring. drf-spectacular reads
``view.__doc__`` for an endpoint's ``description``, and Python does not
inherit ``__doc__`` -- a docstring on the subclass would replace several
paragraphs of published API documentation with a sentence about tenancy or DI
plumbing. What each class is *for* is this module docstring's job; what the
endpoint *does* is the package's.
"""

from typing import TYPE_CHECKING, Annotated

from dependency_injector.wiring import Provide, inject
from vinta_billing import views as billing_views

# Aliased, not re-exported unchanged: subclassed below with the tenant mixin,
# and the alias avoids shadowing this module's own `BillingProfileViewSet`.
from vinta_billing.views import BillingProfileViewSet as _PackageBillingProfileViewSet

from payments.seams.view_scoping import BillingTenantScopedViewMixin


if TYPE_CHECKING:
    from vinta_billing.services.dunning_service import DunningService
    from vinta_billing.services.payment_provider_resolver import PaymentProviderResolver
    from vinta_billing.services.payment_service import PaymentService
    from vinta_billing.services.subscription_service import SubscriptionService


class PaymentsViewSet(billing_views.PaymentsViewSet):
    __doc__ = billing_views.PaymentsViewSet.__doc__

    @inject
    def __init__(
        self,
        *args,
        payment_service: Annotated["PaymentService", Provide["payment_service"]],
        subscription_service: Annotated["SubscriptionService", Provide["subscription_service"]],
        dunning_service: Annotated["DunningService", Provide["dunning_service"]],
        **kwargs,
    ):
        super().__init__(
            *args,
            payment_service=payment_service,
            subscription_service=subscription_service,
            dunning_service=dunning_service,
            **kwargs,
        )


class BillingProfileViewSet(BillingTenantScopedViewMixin, _PackageBillingProfileViewSet):
    __doc__ = _PackageBillingProfileViewSet.__doc__


class PaymentProviderViewSet(BillingTenantScopedViewMixin, billing_views.PaymentProviderViewSet):
    __doc__ = billing_views.PaymentProviderViewSet.__doc__

    @inject
    def __init__(
        self,
        *args,
        payment_provider_resolver: Annotated[
            "PaymentProviderResolver", Provide["payment_provider_resolver"]
        ],
        **kwargs,
    ):
        super().__init__(*args, payment_provider_resolver=payment_provider_resolver, **kwargs)


class DefaultPaymentProviderView(billing_views.DefaultPaymentProviderView):
    __doc__ = billing_views.DefaultPaymentProviderView.__doc__

    @inject
    def __init__(
        self,
        *args,
        payment_provider_resolver: Annotated[
            "PaymentProviderResolver", Provide["payment_provider_resolver"]
        ],
        **kwargs,
    ):
        super().__init__(*args, payment_provider_resolver=payment_provider_resolver, **kwargs)
