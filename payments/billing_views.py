"""Phase 9: the self-serve billing surface -- an organization on the free plan
chooses a paid plan, pays, and sees its limits lift with no support or
engineering intervention (spec objective 2).

Every viewset here resolves the *billing root* (``resolve_billing_root``) for
whichever action needs a ``Subscription`` -- a reseller child asks the same
questions its root would answer, exactly like ``EntitlementService``. Reads
(usage, plan catalog, subscription detail) stay open to any authenticated
member; purchase/change actions are gated by ``IsBillingOwnerOrAdmin``
(admin-or-billing-owner-of-this-org, or an acting reseller root -- see that
permission's docstring).
"""

import logging
from typing import TYPE_CHECKING, Annotated

from django.db.models import QuerySet

from dependency_injector.wiring import Provide, inject
from django_virtual_models.generic_views import GenericVirtualModelViewMixin
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.viewsets import GenericViewSet, ViewSet

from common.utils.view_utils import TenantScopedViewMixin
from organizations.models import Organization
from organizations.permissions import IsBillingOwnerOrAdmin
from payments.billing_constants import BillingState, LimitedResource
from payments.exceptions import (
    AddOnNotPurchasableError,
    PaymentTokenRequiredError,
    UnconfirmedPlanChangeError,
)
from payments.filtersets import BillingPlanFilterSet, SubscriptionAddOnFilterSet
from payments.models import BillingPlan, Subscription, SubscriptionAddOn
from payments.serializers import (
    AddOnPurchaseRequestSerializer,
    BillingPlanSerializer,
    ChangePlanRequestSerializer,
    SubscriptionAddOnSerializer,
    SubscriptionSerializer,
    UsageResponseSerializer,
)
from payments.services.subscription_service import resolve_billing_root


if TYPE_CHECKING:
    from payments.services.entitlement_service import EntitlementService
    from payments.services.subscription_service import SubscriptionService


logger = logging.getLogger(__name__)


def _require_organization(request) -> Organization:
    """``request.organization``, or ``PermissionDenied`` -- every action in this
    module needs an active organization to resolve a billing root against."""
    organization = getattr(request, "organization", None)
    if organization is None:
        raise PermissionDenied("An active organization is required to manage billing.")
    return organization


class BillingPlanViewSet(mixins.ListModelMixin, GenericVirtualModelViewMixin, GenericViewSet):
    """``GET /billing/plans/`` -- the active catalog, with limits and
    entitlements, so a client can render an upgrade picker in one round trip."""

    serializer_class = BillingPlanSerializer
    queryset = BillingPlan.objects.filter(is_active=True)
    filterset_class = BillingPlanFilterSet
    permission_classes = (IsAuthenticated,)

    @extend_schema(summary="List active billing plans", responses={200: BillingPlanSerializer})
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)


class BillingUsageViewSet(TenantScopedViewMixin, ViewSet):
    """``GET /billing/usage/`` -- current usage against effective limits, per
    resource, plus ``billing_state``. Resolved at the billing root, same as
    every other read in this app.

    Phase 12's "pull" half of Use-case 8 ("an organization can see where it
    stands"). It reads usage through the identical
    ``EntitlementService.get_effective_limit`` / ``get_current_usage`` methods
    ``check_limit`` / ``check_postpaid_allowance`` count against, and that
    ``payments.services.usage_warning_service.UsageWarningService`` (the
    "push" half -- proactive approaching-limit notifications) also reads its
    ceiling from -- so this endpoint, the enforcement guards, and the beat
    warning can never disagree about a number.

    No permission beyond ``IsAuthenticated``, deliberately -- a read never
    blocks, including for a ``RESTRICTED`` organization (Phase 11 blocks
    writes and pauses sync, never reads; an organization must be able to see
    exactly what it needs to resolve before it can act on it).
    """

    permission_classes = (IsAuthenticated,)
    #: Not a `GenericAPIView`, so drf-spectacular cannot infer this from
    #: `get_serializer_class()` -- declared explicitly so schema generation
    #: does not fall back to "ignoring view".
    serializer_class = UsageResponseSerializer

    @inject
    def __init__(
        self,
        *args,
        entitlement_service: Annotated["EntitlementService", Provide["entitlement_service"]],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.entitlement_service = entitlement_service

    @extend_schema(summary="Get current usage against effective limits", request=None)
    @action(methods=["get"], detail=False, url_path="", url_name="retrieve")
    def retrieve_usage(self, request, *args, **kwargs):
        organization = _require_organization(request)
        root = resolve_billing_root(organization)
        subscription = Subscription.objects.filter(organization=root).first()
        billing_state = (
            subscription.billing_state if subscription is not None else BillingState.FREE
        )

        limits = []
        for resource_key in LimitedResource.values:
            effective_limit = self.entitlement_service.get_effective_limit(
                organization, resource_key
            )
            current_usage = self.entitlement_service.get_current_usage(organization, resource_key)
            limits.append(
                {
                    "resource_key": resource_key,
                    "kind": effective_limit.kind,
                    "limit_value": effective_limit.limit_value,
                    "current_usage": current_usage,
                    "overage_unit_price": effective_limit.overage_unit_price,
                }
            )

        serializer = UsageResponseSerializer({"billing_state": billing_state, "limits": limits})
        return Response(serializer.data)


class SubscriptionViewSet(TenantScopedViewMixin, GenericVirtualModelViewMixin, GenericViewSet):
    """``GET /billing/subscription/``, ``POST .../change-plan/``,
    ``POST .../cancel/``."""

    serializer_class = SubscriptionSerializer
    queryset = Subscription.objects.all()
    permission_classes = (IsAuthenticated,)

    #: Purchase/change actions require billing-owner-or-admin (plan's Phase 9
    #: permission rule); plain reads stay open to any authenticated member.
    write_actions = ("change_plan", "cancel")
    #: The write actions drive real provider round trips (``change_plan`` a
    #: charge); throttle them per the same ``ScopedRateThrottle`` bound-abuse
    #: rationale as the inbound webhook endpoints, while leaving reads unthrottled.
    throttle_scope = "billing-write"

    @inject
    def __init__(
        self,
        *args,
        subscription_service: Annotated["SubscriptionService", Provide["subscription_service"]],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.subscription_service = subscription_service

    def get_permissions(self):
        if self.action in self.write_actions:
            return [IsAuthenticated(), IsBillingOwnerOrAdmin()]
        return super().get_permissions()

    def get_throttles(self):
        if self.action in self.write_actions:
            return [ScopedRateThrottle()]
        return super().get_throttles()

    def get_queryset(self) -> QuerySet[Subscription]:
        # Chain the organization filter on top of the virtual-model-optimized base
        # queryset, mirroring BillingProfileViewSet.get_queryset().
        queryset = super().get_queryset()
        organization = getattr(self.request, "organization", None)
        if organization is None:
            return queryset.none()
        return queryset.filter(organization=resolve_billing_root(organization))

    def get_subscription(self, *, check_object_perms: bool = False) -> Subscription:
        organization = _require_organization(self.request)
        subscription = self.get_queryset().first()
        if subscription is None:
            raise NotFound("This organization has no subscription.")
        if check_object_perms:
            # `has_permission` alone cannot decide *which* organization a write
            # is for -- `request.organization` is not resolved yet at that
            # point in `TenantScopedViewMixin.initial()`'s ordering (see
            # `IsBillingOwnerOrAdmin`'s docstring). This is the object-level
            # check against the actually-resolved billing root.
            self.check_object_permissions(self.request, resolve_billing_root(organization))
        return subscription

    @extend_schema(
        summary="Retrieve the org's subscription", responses={200: SubscriptionSerializer}
    )
    @action(methods=["get"], detail=False, url_path="", url_name="retrieve")
    def retrieve_subscription(self, request, *args, **kwargs):
        subscription = self.get_subscription()
        return Response(self.get_serializer(subscription).data)

    @extend_schema(
        summary="Upgrade or downgrade the org's plan",
        request=ChangePlanRequestSerializer,
        responses={200: SubscriptionSerializer},
    )
    @action(methods=["post"], detail=False, url_path="change-plan", url_name="change-plan")
    def change_plan(self, request, *args, **kwargs):
        subscription = self.get_subscription(check_object_perms=True)
        request_serializer = ChangePlanRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        data = request_serializer.validated_data

        plan = BillingPlan.objects.filter(slug=data["plan_slug"], is_active=True).first()
        if plan is None:
            raise NotFound(f"No active billing plan with slug {data['plan_slug']!r}.")

        try:
            self.subscription_service.request_plan_change(
                subscription,
                plan,
                data["billing_interval"],
                payment_token=data.get("payment_token", ""),
                idempotency_key=data["idempotency_key"],
            )
        except PaymentTokenRequiredError as error:
            raise ValidationError({"payment_token": str(error)}) from error
        except UnconfirmedPlanChangeError as error:
            # A different plan change is already in flight and unconfirmed --
            # 409 Conflict rather than a validation error on any one field.
            return Response({"detail": str(error)}, status=status.HTTP_409_CONFLICT)

        # Re-fetched through the virtual-model-optimized queryset rather than
        # serializing the plain instance `request_plan_change` returns --
        # `SubscriptionSerializer` prefetches `plan`/`add_ons`, and serializing
        # an un-prefetched instance would N+1.
        return Response(self.get_serializer(self.get_subscription()).data)

    @extend_schema(
        summary="Cancel the org's subscription",
        request=None,
        responses={200: SubscriptionSerializer},
    )
    @action(methods=["post"], detail=False, url_path="cancel", url_name="cancel")
    def cancel(self, request, *args, **kwargs):
        subscription = self.get_subscription(check_object_perms=True)
        self.subscription_service.cancel_subscription(subscription)
        return Response(self.get_serializer(self.get_subscription()).data)


class AddOnViewSet(TenantScopedViewMixin, GenericViewSet):
    """``POST /billing/add-ons/`` (purchase capacity), ``DELETE
    /billing/add-ons/{id}/`` (stop a recurring add-on from renewing).

    ``SubscriptionAddOnSerializer`` is a plain ``ModelSerializer`` -- no
    nested relation heavy enough to warrant a virtual model (see
    ``payments/virtual_models.py``) -- so this does not mix in
    ``GenericVirtualModelViewMixin``, unlike ``SubscriptionViewSet``.
    """

    serializer_class = SubscriptionAddOnSerializer
    queryset = SubscriptionAddOn.objects.all()
    filterset_class = SubscriptionAddOnFilterSet
    permission_classes = (IsAuthenticated, IsBillingOwnerOrAdmin)
    #: ``create`` drives a real one-time provider charge; throttle it (and the
    #: recurrence-cancel ``destroy`` write) with the same shared ``billing-write``
    #: scope the plan-change endpoint uses.
    throttle_scope = "billing-write"
    write_actions = ("create", "destroy")

    @inject
    def __init__(
        self,
        *args,
        subscription_service: Annotated["SubscriptionService", Provide["subscription_service"]],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.subscription_service = subscription_service

    def get_throttles(self):
        if self.action in self.write_actions:
            return [ScopedRateThrottle()]
        return super().get_throttles()

    def get_queryset(self) -> QuerySet[SubscriptionAddOn]:
        queryset = super().get_queryset()
        organization = getattr(self.request, "organization", None)
        if organization is None:
            return queryset.none()
        return queryset.filter(subscription__organization=resolve_billing_root(organization))

    def _get_subscription(self, organization: Organization) -> Subscription:
        subscription = Subscription.objects.filter(
            organization=resolve_billing_root(organization)
        ).first()
        if subscription is None:
            raise NotFound("This organization has no subscription.")
        return subscription

    @extend_schema(
        summary="Purchase additional capacity",
        request=AddOnPurchaseRequestSerializer,
        responses={201: SubscriptionAddOnSerializer},
    )
    def create(self, request, *args, **kwargs):
        organization = _require_organization(request)
        billing_root = resolve_billing_root(organization)
        # See `SubscriptionViewSet.get_subscription`'s comment: `has_permission`
        # cannot know *which* organization this write is for, since
        # `request.organization` is not resolved yet at that point --
        # `has_object_permission` is the real gate, run here against the
        # resolved billing root.
        self.check_object_permissions(request, billing_root)
        subscription = self._get_subscription(organization)

        request_serializer = AddOnPurchaseRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        data = request_serializer.validated_data

        try:
            add_on = self.subscription_service.purchase_add_on(
                subscription,
                data["resource_key"],
                data["quantity"],
                data["is_recurring"],
                data["idempotency_key"],
                data.get("payment_token", ""),
            )
        except AddOnNotPurchasableError as error:
            raise ValidationError({"resource_key": str(error)}) from error

        return Response(self.get_serializer(add_on).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="Cancel a recurring add-on at period end",
        request=None,
        responses={200: SubscriptionAddOnSerializer},
    )
    def destroy(self, request, *args, **kwargs):
        add_on = self.get_object()
        add_on = self.subscription_service.cancel_add_on(add_on)
        return Response(self.get_serializer(add_on).data)
