"""This project's mounting of ``vinta_billing.billing_views``.

Same two host-owned concerns as ``payments/views.py`` -- read that module's
docstring first. The endpoints, their serializers, their querysets and their
~900 lines of action bodies are the package's; what is here is the
``X-Organization-Id`` scoping and the ``di_core`` service wiring.

Every viewset below carries a bare ``__doc__ = <package class>.__doc__``
assignment rather than its own docstring. drf-spectacular reads
``view.__doc__`` for an endpoint's ``description``, and Python does not
inherit ``__doc__`` -- a docstring on the subclass would replace several
paragraphs of published API documentation with a sentence about tenancy
plumbing. What each class is *for* is this module docstring's job; what the
endpoint *does* is the package's.
"""

from typing import TYPE_CHECKING, Annotated

from dependency_injector.wiring import Provide, inject
from vinta_billing import billing_views

# Re-exported unchanged: the plan catalogue is the same for every caller, takes
# no service, and the package's own class is deliberately not tenant-scoped.
from vinta_billing.billing_views import BillingPlanViewSet  # noqa: F401

from payments.seams.view_scoping import BillingTenantScopedViewMixin


if TYPE_CHECKING:
    from vinta_billing.services.entitlement_service import EntitlementService
    from vinta_billing.services.subscription_service import SubscriptionService


class _EntitlementServiceInjected:
    """Forwards ``di_core``'s ``entitlement_service`` into a package viewset.

    Three viewsets below take exactly this one service, so the ``@inject``
    constructor is written once. It is a plain mixin rather than a shared base
    class because each of those viewsets has its own package base, and the
    ``super()`` call here has to land on whichever one the subclass names.
    """

    @inject
    def __init__(
        self,
        *args,
        entitlement_service: Annotated["EntitlementService", Provide["entitlement_service"]],
        **kwargs,
    ):
        super().__init__(*args, entitlement_service=entitlement_service, **kwargs)


class _SubscriptionServiceInjected:
    """Forwards ``di_core``'s ``subscription_service`` into a package viewset.
    See :class:`_EntitlementServiceInjected`."""

    @inject
    def __init__(
        self,
        *args,
        subscription_service: Annotated["SubscriptionService", Provide["subscription_service"]],
        **kwargs,
    ):
        super().__init__(*args, subscription_service=subscription_service, **kwargs)


class BillingUsageViewSet(
    BillingTenantScopedViewMixin, _EntitlementServiceInjected, billing_views.BillingUsageViewSet
):
    __doc__ = billing_views.BillingUsageViewSet.__doc__


class BillingPeriodViewSet(
    BillingTenantScopedViewMixin, _EntitlementServiceInjected, billing_views.BillingPeriodViewSet
):
    __doc__ = billing_views.BillingPeriodViewSet.__doc__


class MeteredOccurrenceViewSet(
    BillingTenantScopedViewMixin,
    _EntitlementServiceInjected,
    billing_views.MeteredOccurrenceViewSet,
):
    __doc__ = billing_views.MeteredOccurrenceViewSet.__doc__


class SubscriptionViewSet(
    BillingTenantScopedViewMixin, _SubscriptionServiceInjected, billing_views.SubscriptionViewSet
):
    __doc__ = billing_views.SubscriptionViewSet.__doc__


class AddOnViewSet(
    BillingTenantScopedViewMixin, _SubscriptionServiceInjected, billing_views.AddOnViewSet
):
    __doc__ = billing_views.AddOnViewSet.__doc__
