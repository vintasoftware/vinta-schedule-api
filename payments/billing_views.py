"""The self-serve billing surface -- an organization on the free plan
chooses a paid plan, pays, and sees its limits lift with no support or
engineering intervention.

Every viewset here resolves the *billing root* (``resolve_billing_root``) for
whichever action needs a ``Subscription`` -- a reseller child asks the same
questions its root would answer, exactly like ``EntitlementService``. Reads
(usage, plan catalog, subscription detail) stay open to any authenticated
member; purchase/change actions require ``IsBillingOwnerOrAdmin``
(admin-or-billing-owner-of-this-org, or an acting reseller root -- see that
permission's docstring).
"""

import logging
from collections.abc import Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated

from django.db.models import Prefetch, QuerySet, Sum
from django.utils import timezone

from dependency_injector.wiring import Provide, inject
from django_virtual_models.generic_views import GenericVirtualModelViewMixin
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import mixins, serializers, status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.viewsets import GenericViewSet, ViewSet

from calendar_integration.models import CalendarEvent, CalendarOwnership
from common.utils.view_utils import TenantScopedViewMixin
from organizations.models import Organization
from organizations.permissions import IsBillingOwnerOrAdmin
from payments.billing_constants import BillingState, LimitedResource
from payments.filtersets import (
    BillingPeriodSummaryFilterSet,
    BillingPlanFilterSet,
    MeteredOccurrenceFilterSet,
    SubscriptionAddOnFilterSet,
)
from payments.models import (
    BillingPeriodSummary,
    BillingPlan,
    MeteredOccurrence,
    Subscription,
    SubscriptionAddOn,
    SubscriptionPlanLimit,
)
from payments.pagination import LargeLimitOffsetPagination
from payments.serializers import (
    AddOnPurchaseRequestSerializer,
    BillingPeriodSummaryDetailSerializer,
    BillingPeriodSummarySerializer,
    BillingPlanSerializer,
    ChangePlanRequestSerializer,
    MeteredOccurrenceSerializer,
    RetryPaymentRequestSerializer,
    SubscriptionAddOnSerializer,
    SubscriptionSerializer,
    UsageResponseSerializer,
)
from payments.services.subscription_service import (
    current_billing_period_start,
    resolve_billing_period,
    resolve_billing_root,
)


if TYPE_CHECKING:
    from payments.services.entitlement_service import EntitlementService
    from payments.services.subscription_service import SubscriptionService


logger = logging.getLogger(__name__)

#: The shared contract body every ``BillingError`` renders through
#: ``common.exception_handlers.vinta_exception_handler`` (``payments.exceptions
#: .BillingError.as_error_body``) -- documented once here and reused across every
#: ``extend_schema`` response that surfaces a billing error, rather than each
#: action inlining the same two fields.
BILLING_ERROR_BODY_SERIALIZER = inline_serializer(
    name="BillingErrorBody",
    fields={
        "code": serializers.CharField(),
        "detail": serializers.CharField(),
    },
)


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
    resource, plus ``billing_state``, the plan snapshot, the current billing
    period's bounds, the plan/add-on split of each ceiling, per-organization
    attribution across the caller's pooled subtree, and the overage accrued so
    far this cycle. Resolved at the billing root, same as every other read in
    this app.

    The "pull" half of "an organization can see where it stands". It reads
    usage through ``EntitlementService.effective_limit_from_resolved`` /
    ``usage_breakdown_for_root`` -- pre-resolved entry points onto the
    identical ``get_effective_limit`` / ``get_current_usage`` implementation
    ``check_limit`` / ``check_postpaid_allowance`` count against, and that
    ``payments.services.usage_warning_service.UsageWarningService`` (the
    "push" half -- proactive approaching-limit notifications) also reads its
    ceiling from -- so this endpoint, the enforcement checks, and the beat
    warning can never disagree about a number.

    No permission beyond ``IsAuthenticated``, deliberately -- a read never
    blocks, including for a ``RESTRICTED`` organization (a RESTRICTED
    organization has its writes blocked and sync paused, never its reads; an
    organization must be able to see exactly what it needs to resolve before it
    can act on it).
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
    def retrieve_usage(self, request: Request, *args: object, **kwargs: object) -> Response:
        # Resolves the billing root, the pooled subtree, and the subscription
        # **once** and threads all three through the per-resource loop below via
        # `EntitlementService`'s pre-resolved public entry points
        # (`effective_limit_for_subscription` / `usage_breakdown_for_root`) --
        # exactly the pattern `CycleCloseService._persist_statement` already
        # established for the same reason. Before this, the loop called
        # `get_effective_limit`/`get_current_usage` per resource, each of which
        # independently re-walked the `parent` chain and re-ran the subtree BFS:
        # sixteen root resolutions and eight subtree walks for eight resources.
        # Now it is one of each, regardless of how many `LimitedResource` members
        # exist. Not a docstring -- this is an implementation note for the next
        # reader of this method, not customer-facing API documentation, and
        # drf-spectacular would otherwise pull it into the schema in place of the
        # class docstring above.
        organization = _require_organization(request)
        root = resolve_billing_root(organization)
        # `select_related("plan")` folds the plan-snapshot lookup into this one
        # query instead of a second round trip when a subscription exists.
        subscription = Subscription.objects.select_related("plan").filter(organization=root).first()
        billing_state = (
            subscription.billing_state if subscription is not None else BillingState.FREE
        )

        # `root` is already a billing root by construction, so resolving the pool
        # from it (rather than from `organization`) costs `resolve_billing_root`
        # nothing further -- `is_billing_root(root)` short-circuits before any
        # `parent` access -- and this is the pool's *only* resolution for the
        # whole response.
        pooled_organization_ids = self.entitlement_service.get_pooled_organization_ids(root)
        organization_names = dict(
            Organization.objects.filter(pk__in=pooled_organization_ids).values_list("pk", "name")
        )

        plan: dict[str, str] | None = None
        billing_period: dict[str, object] | None = None
        estimated_overage_total = Decimal("0.0000")
        # Batched once for the whole loop, not once per resource: the plan-limit
        # row and the active add-on total behind `included_in_plan`/
        # `add_on_quantity` below.
        plan_limit_by_resource: dict[str, SubscriptionPlanLimit] = {}
        add_on_quantity_by_resource: dict[str, int] = {}
        if subscription is not None:
            plan = {
                "slug": subscription.plan.slug,
                "name": subscription.plan.name,
                "currency": subscription.plan.currency,
            }
            # Same underlying derivation `current_billing_period_start` uses
            # (`resolve_billing_period(subscription, timezone.now())[0]`) -- called
            # directly here, once, so `billing_period`'s own start and end come
            # from a single resolution rather than two separate `timezone.now()`
            # reads. This does **not** close the boundary race end to end: the
            # `event_occurrences` row below routes through `usage_breakdown_for_root`
            # into `_count_event_occurrences`, which independently calls
            # `current_billing_period_start(subscription)` -- a second, later
            # `timezone.now()` read in the same request. A request that straddles
            # a period boundary between the two reads can still see
            # `estimated_overage_total` and the `event_occurrences` row disagree by
            # one cycle. Closing that requires a way to inject a period override
            # into the counter signature, which Phase 1's `UsageContext` does not
            # support -- out of scope here.
            period_start, period_end = resolve_billing_period(subscription, timezone.now())
            billing_period = {"start": period_start, "end": period_end}
            estimated_overage_total = (
                MeteredOccurrence.objects.for_billing_period(subscription.pk, period_start)
                .for_organizations(pooled_organization_ids)
                .overage_total()
            )
            plan_limit_by_resource = {
                limit.resource_key: limit for limit in subscription.limits.all()
            }
            add_on_quantity_by_resource = dict(
                subscription.add_ons.filter(is_active=True)
                .values("resource_key")
                .annotate(total=Sum("quantity"))
                .values_list("resource_key", "total")
            )

        limits = []
        for resource_key in LimitedResource.values:
            plan_limit_row = plan_limit_by_resource.get(resource_key)
            add_on_quantity_for_ceiling = add_on_quantity_by_resource.get(resource_key, 0)
            # Pre-resolved entry point: `plan_limit_row`/`add_on_quantity_for_ceiling`
            # were already fetched in bulk above, so this does not re-run the
            # per-resource `SubscriptionPlanLimit` lookup and `Sum` aggregate
            # `effective_limit_for_subscription` would otherwise repeat here.
            effective_limit = self.entitlement_service.effective_limit_from_resolved(
                resource_key, plan_limit_row, add_on_quantity_for_ceiling
            )
            usage_breakdown = self.entitlement_service.usage_breakdown_for_root(
                root, resource_key, subscription, pooled_organization_ids=pooled_organization_ids
            )
            # Structurally the same sum `_count_usage` performs -- derived from the
            # breakdown just fetched rather than a second, independent count.
            current_usage = sum(usage_breakdown.values())

            if plan_limit_row is None or plan_limit_row.limit_value is None:
                # Mirrors `_effective_limit_for_subscription`'s own fail-open
                # branches exactly: no plan-limit row, or an explicitly unlimited
                # one, both resolve `limit_value` to `None` without ever
                # consulting add-ons for the *ceiling*. `add_on_quantity` still
                # reports what was actually purchased when a (unlimited) row
                # exists, since it is informational and does not change the
                # ceiling either way.
                included_in_plan = None
                add_on_quantity = (
                    add_on_quantity_by_resource.get(resource_key, 0)
                    if plan_limit_row is not None
                    else 0
                )
            else:
                included_in_plan = plan_limit_row.limit_value
                add_on_quantity = add_on_quantity_by_resource.get(resource_key, 0)

            by_organization = [
                {
                    "organization_id": organization_id,
                    "name": organization_names.get(organization_id, ""),
                    "usage": usage,
                }
                for organization_id, usage in sorted(usage_breakdown.items())
            ]

            limits.append(
                {
                    "resource_key": resource_key,
                    "kind": effective_limit.kind,
                    "limit_value": effective_limit.limit_value,
                    "current_usage": current_usage,
                    "overage_unit_price": effective_limit.overage_unit_price,
                    "included_in_plan": included_in_plan,
                    "add_on_quantity": add_on_quantity,
                    "by_organization": by_organization,
                }
            )

        serializer = UsageResponseSerializer(
            {
                "billing_state": billing_state,
                "billing_root_organization_id": root.pk,
                "plan": plan,
                "billing_period": billing_period,
                "estimated_overage_total": estimated_overage_total,
                "limits": limits,
            }
        )
        return Response(serializer.data)


class BillingPeriodViewSet(
    TenantScopedViewMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet
):
    """``GET /billing/usage/periods/`` and ``GET /billing/usage/periods/{id}/``
    -- the durable statements ``CycleCloseService`` writes at cycle close (see
    ``BillingPeriodSummary``'s docstring). List and detail are bundled
    deliberately: they share a queryset, a permission, and a serializer tree
    (see the plan's "Bundled phase granularity" decision) rather than shipping
    as two PRs whose second one is fifty lines.

    Scoped to the caller's resolved pool exactly like ``BillingUsageViewSet``:
    ``resolve_billing_root`` then ``get_pooled_organization_ids``, both
    resolved once in ``get_queryset()``. A pk outside that pool is filtered out
    of the queryset before ``get_object()`` ever runs, so it 404s -- never
    403 -- and this endpoint never confirms the existence of another tenant's
    statement.

    ``IsAuthenticated`` only, matching ``GET /billing/usage/``'s
    read-never-blocks rule: a closed statement is exactly the kind of read an
    organization needs in order to resolve billing, including while
    ``RESTRICTED``.

    History is forward-only (see the plan's Non-goals / Risk & Rollout Notes):
    an organization with no closed periods yet gets ``200`` with an empty list,
    never a ``404`` -- there is nothing wrong with that organization, cycle
    close simply has not run for it yet. A caller with **no active
    organization** (``request.organization is None``) is a different state --
    there is no pool to resolve a billing root against at all -- and gets
    ``403``, matching ``GET /billing/usage/``'s ``_require_organization`` rule
    rather than the empty-list state above.
    """

    queryset = BillingPeriodSummary.objects.all()
    filterset_class = BillingPeriodSummaryFilterSet
    permission_classes = (IsAuthenticated,)

    @inject
    def __init__(
        self,
        *args,
        entitlement_service: Annotated["EntitlementService", Provide["entitlement_service"]],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.entitlement_service = entitlement_service

    def get_serializer_class(self) -> type[BaseSerializer]:
        if self.action == "retrieve":
            return BillingPeriodSummaryDetailSerializer
        return BillingPeriodSummarySerializer

    def get_queryset(self) -> QuerySet[BillingPeriodSummary]:
        organization = _require_organization(self.request)
        root = resolve_billing_root(organization)
        pooled_organization_ids = self.entitlement_service.get_pooled_organization_ids(root)
        queryset = BillingPeriodSummary.objects.for_organizations(pooled_organization_ids)
        if self.action == "retrieve":
            # Bounded query count for the detail action: one query for the
            # statement plus one for its resources, not one per resource row.
            queryset = queryset.prefetch_related("resources")
        return queryset

    @extend_schema(
        summary="List closed billing period statements",
        responses={200: BillingPeriodSummarySerializer},
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        summary="Retrieve one closed billing period statement",
        responses={200: BillingPeriodSummaryDetailSerializer},
    )
    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        # `instance.resources` is already prefetched by `get_queryset()` for
        # the retrieve action, so this walks the prefetch cache rather than
        # issuing a query per resource row. Bounded by pool size, not by
        # resource-row count: one extra query total, resolving every
        # organization pk referenced across all resource rows in one batch --
        # the same pattern `BillingUsageViewSet.retrieve_usage` already uses
        # for `organization_names`.
        organization_ids = {
            int(organization_id)
            for resource in instance.resources.all()
            for organization_id in resource.by_organization
        }
        organization_names = dict(
            Organization.objects.filter(pk__in=organization_ids).values_list("pk", "name")
        )
        context = self.get_serializer_context()
        context["organization_names"] = organization_names
        serializer = self.get_serializer(instance, context=context)
        return Response(serializer.data)


class MeteredOccurrenceViewSet(TenantScopedViewMixin, mixins.ListModelMixin, GenericViewSet):
    """``GET /billing/usage/occurrences/`` -- the line-item ledger behind
    post-paid charges: every ``MeteredOccurrence`` row in the caller's pooled
    billing subtree, paginated and filterable by period, allowance side,
    organization, and occurrence-start range, so a customer disputing an
    invoice can tie every unit of money to a specific occurrence.

    **Stricter than every other read in this module.** ``BillingUsageViewSet``
    and ``BillingPeriodViewSet`` stay open to any authenticated member
    (``IsAuthenticated`` alone); this one additionally requires
    ``IsBillingOwnerOrAdmin``. A ledger row carries an ``event_id`` and an exact
    ``occurrence_start`` -- that is calendar content, and it spans every
    calendar in the caller's pooled subtree, including ones the caller has no
    membership scope on. A count is not. See the plan's Guiding Decisions.

    ``check_object_permissions`` is called explicitly in ``list()`` against the
    resolved billing root -- the same two-step dance
    ``SubscriptionViewSet.get_subscription`` and ``AddOnViewSet.create`` already
    perform, and for the same reason their comments document:
    ``has_permission`` cannot know *which* organization this read is for: the
    read is against the billing **root**, which is frequently an ancestor of the
    organization the request resolved, and only the caller of
    ``check_object_permissions`` knows which one that is (see
    ``IsBillingOwnerOrAdmin``'s docstring).
    """

    serializer_class = MeteredOccurrenceSerializer
    queryset = MeteredOccurrence.objects.none()
    filterset_class = MeteredOccurrenceFilterSet
    permission_classes = (IsAuthenticated, IsBillingOwnerOrAdmin)
    pagination_class = LargeLimitOffsetPagination

    @inject
    def __init__(
        self,
        *args,
        entitlement_service: Annotated["EntitlementService", Provide["entitlement_service"]],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.entitlement_service = entitlement_service

    def get_queryset(self) -> QuerySet[MeteredOccurrence]:
        organization = _require_organization(self.request)
        root = resolve_billing_root(organization)
        # The call below re-resolves root internally; passing the already-resolved root is a deliberate no-op.
        pooled_organization_ids = self.entitlement_service.get_pooled_organization_ids(root)
        # Stashed on the request so `MeteredOccurrenceFilterSet.filter_organization`
        # can validate the `organization` filter value against the caller's pool
        # without re-resolving it or reaching into DI itself.
        self.request.pooled_organization_ids = pooled_organization_ids  # type: ignore[attr-defined]

        queryset = MeteredOccurrence.objects.for_organizations(pooled_organization_ids).order_by(
            "-occurrence_start"
        )
        if "billing_period_start" in self.request.query_params:
            # An explicit period filter is coming; let `MeteredOccurrenceFilterSet`
            # narrow it below rather than also constraining by the current period.
            return queryset

        subscription = Subscription.objects.filter(organization=root).first()
        if subscription is None:
            return queryset.none()
        period_start = current_billing_period_start(subscription)
        return queryset.for_billing_period(subscription.pk, period_start)

    @extend_schema(
        summary="List metered occurrences behind post-paid charges",
        responses={200: MeteredOccurrenceSerializer},
    )
    def list(self, request: Request, *args: object, **kwargs: object) -> Response:
        organization = _require_organization(request)
        root = resolve_billing_root(organization)
        # The real, object-level gate -- see the class docstring and
        # `IsBillingOwnerOrAdmin`'s docstring for why this cannot live in
        # `has_permission`.
        self.check_object_permissions(request, root)

        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        occurrences = page if page is not None else list(queryset)

        # Batched once per page/response, never once per row: organization
        # names (mirrors `BillingUsageViewSet`/`BillingPeriodViewSet`'s
        # `organization_names` pattern) and event/calendar/owner enrichment
        # (see `_resolve_events`). Stashed on `self` rather than passed as an
        # explicit `context=` kwarg to `get_serializer` below, because
        # `GenericAPIView.get_serializer` builds its own `context` via
        # `get_serializer_context()` and passing both raises a duplicate-kwarg
        # `TypeError`.
        organization_ids = {occurrence.organization_id for occurrence in occurrences}
        self._organization_names = dict(
            Organization.objects.filter(pk__in=organization_ids).values_list("pk", "name")
        )
        self._event_map = self._resolve_events(occurrences)

        serializer = self.get_serializer(occurrences, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    def get_serializer_context(self) -> dict[str, object]:
        # `GenericAPIView.get_serializer_context` is typed `Mapping[str, Any]`
        # (read-only) -- copy into a plain `dict` before adding keys.
        context: dict[str, object] = dict(super().get_serializer_context())
        context["organization_names"] = getattr(self, "_organization_names", {})
        context["event_map"] = getattr(self, "_event_map", {})
        return context

    def _resolve_events(self, occurrences: Sequence[MeteredOccurrence]) -> dict[int, CalendarEvent]:
        """Batched, per-page event enrichment: one query for the events (with
        their calendars, via ``select_related``) and one for the calendars'
        ownerships (with each owner's membership/user/profile joined in that
        same query, via a ``Prefetch`` queryset) -- never a per-row lookup.

        ``event_id`` is a soft reference (``BigIntegerField``, not a
        ``ForeignKey`` -- see the ``MeteredOccurrence`` model docstring), so
        this can never be a ``select_related`` off ``MeteredOccurrence``
        itself; it is always this second, explicit, batched query. Missing
        event ids (a deleted event) are simply absent from the returned map --
        the serializer renders ``event: null`` for those rows.
        """
        event_ids = {occurrence.event_id for occurrence in occurrences}
        if not event_ids:
            return {}

        # Scoped to the same pool `get_queryset()` already resolved for this
        # request (stashed there) -- an event enriching a row this endpoint is
        # already allowed to show can only belong to an organization in that
        # same pool.
        pooled_organization_ids = getattr(self.request, "pooled_organization_ids", ())
        # ``unscoped()`` on both: the pool spans a reseller subtree, which no
        # single-organization binding can express; ``pooled_organization_ids`` is
        # the tenant boundary and is applied in each filter.
        events = (
            CalendarEvent.objects.unscoped()
            .filter(pk__in=event_ids, organization_id__in=pooled_organization_ids)
            .select_related("calendar")
            .prefetch_related(
                Prefetch(
                    "calendar__ownerships",
                    queryset=CalendarOwnership.objects.unscoped()
                    .filter(organization_id__in=pooled_organization_ids)
                    .select_related("membership__user__profile"),
                )
            )
        )
        return {event.pk: event for event in events}


class SubscriptionViewSet(TenantScopedViewMixin, GenericVirtualModelViewMixin, GenericViewSet):
    """``GET /billing/subscription/``, ``POST .../change-plan/``,
    ``POST .../cancel/``, ``POST .../retry-payment/``."""

    serializer_class = SubscriptionSerializer
    queryset = Subscription.objects.all()
    permission_classes = (IsAuthenticated,)

    #: Purchase/change actions require billing-owner-or-admin; plain reads stay
    #: open to any authenticated member.
    write_actions = ("change_plan", "cancel", "retry_payment")
    #: The write actions drive real provider round trips (``change_plan`` and
    #: ``retry_payment`` a charge); throttle them per the same
    #: ``ScopedRateThrottle`` bound-abuse rationale as the inbound webhook
    #: endpoints, while leaving reads unthrottled.
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
            # is for: the write acts on the billing *root*, which is frequently
            # an ancestor of the organization the request resolved, and only
            # this line knows which one that is (see `IsBillingOwnerOrAdmin`'s
            # docstring). This is the object-level check against the
            # actually-resolved billing root.
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
        responses={
            200: SubscriptionSerializer,
            400: OpenApiResponse(
                response=BILLING_ERROR_BODY_SERIALIZER,
                description=(
                    "No `payment_token` was supplied and this billing root has no payment "
                    'method on file yet (`code: "payment_token_required"`, '
                    "`PaymentTokenRequiredError`)."
                ),
            ),
            409: OpenApiResponse(
                response=BILLING_ERROR_BODY_SERIALIZER,
                description=(
                    "Either another plan change is already awaiting payment confirmation "
                    '(`code: "unconfirmed_plan_change"`, `UnconfirmedPlanChangeError`), or '
                    "the provider this organization resolves to is not configured in this "
                    'deployment (`code: "payment_provider_not_configured"`, '
                    "`PaymentProviderNotConfiguredError`). Both are mapped centrally in "
                    "`common.exception_handlers.vinta_exception_handler`."
                ),
            ),
        },
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

        # `PaymentTokenRequiredError` (400) and `UnconfirmedPlanChangeError` (409)
        # are rendered centrally by `common.exception_handlers.vinta_exception_handler`
        # -- no local try/except needed here.
        self.subscription_service.request_plan_change(
            subscription,
            plan,
            data["billing_interval"],
            payment_token=data.get("payment_token", ""),
            idempotency_key=data["idempotency_key"],
        )

        # Re-fetched through the virtual-model-optimized queryset rather than
        # serializing the plain instance `request_plan_change` returns --
        # `SubscriptionSerializer` prefetches `plan`/`add_ons`, and serializing
        # an un-prefetched instance would N+1.
        return Response(self.get_serializer(self.get_subscription()).data)

    @extend_schema(
        summary="Cancel the org's subscription",
        request=None,
        responses={
            200: SubscriptionSerializer,
            409: {
                "description": (
                    "The provider this subscription is stamped with is not configured in "
                    "this deployment, so the provider-side cancellation cannot be driven."
                )
            },
        },
    )
    @action(methods=["post"], detail=False, url_path="cancel", url_name="cancel")
    def cancel(self, request, *args, **kwargs):
        subscription = self.get_subscription(check_object_perms=True)
        self.subscription_service.cancel_subscription(subscription)
        return Response(self.get_serializer(self.get_subscription()).data)

    @extend_schema(
        summary="Grace recovery: attach a new payment instrument and collect the outstanding balance",
        request=RetryPaymentRequestSerializer,
        responses={
            200: OpenApiResponse(
                response=SubscriptionSerializer,
                description=(
                    "The new instrument was attached and the outstanding balance was submitted "
                    "for collection at the provider. Recovery is webhook-driven -- the "
                    'subscription in this response is still `"grace"`/`"restricted"`; it moves '
                    'to `"active"` only once the subscription-payment webhook confirms the '
                    "charge."
                ),
            ),
            400: OpenApiResponse(description="`payment_token` or `idempotency_key` is missing."),
            402: OpenApiResponse(
                response=BILLING_ERROR_BODY_SERIALIZER,
                description=(
                    "The new instrument was attached, but the provider declined the charge "
                    'against it, or refused to attempt it at all (`code: "charge_declined"`, '
                    "`ChargeDeclinedError`). Distinct from the over-limit 402 rendered "
                    'elsewhere in this API (`OverLimitError`, `code: "limit_exceeded"`) -- '
                    "both use 402 Payment Required, `code` disambiguates. The subscription "
                    "stays GRACE/RESTRICTED. A subscription can carry more than one "
                    "outstanding invoice; if an earlier one was paid before a later one hit "
                    "this decline, a partial collection may already have occurred -- submit "
                    "a different `payment_token` to retry the remainder, do not assume "
                    "nothing moved."
                ),
            ),
            409: OpenApiResponse(
                response=BILLING_ERROR_BODY_SERIALIZER,
                description=(
                    "One of four conflicts: the subscription is not currently "
                    'GRACE/RESTRICTED (`code: "retry_payment_not_applicable"`, '
                    "`RetryPaymentNotApplicableError`); it has never attached a payment "
                    'instrument at the provider (`code: "subscription_not_attached"`, '
                    "`SubscriptionNotAttachedError` -- such an organization has never paid and "
                    "belongs on `change-plan`'s first-upgrade path instead); the provider "
                    "reports nothing actually owed for this subscription right now "
                    '(`code: "no_outstanding_balance"`, `NoOutstandingBalanceError`); or the '
                    "resolved provider has no verified balance-collection primitive to drive "
                    '(`code: "collection_not_supported"`, `CollectionNotSupportedError` -- '
                    "MercadoPago, as of this writing)."
                ),
            ),
        },
    )
    @action(methods=["post"], detail=False, url_path="retry-payment", url_name="retry-payment")
    def retry_payment(self, request, *args, **kwargs):
        subscription = self.get_subscription(check_object_perms=True)
        request_serializer = RetryPaymentRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        data = request_serializer.validated_data

        # `RetryPaymentNotApplicableError`, `SubscriptionNotAttachedError`,
        # `NoOutstandingBalanceError`, and `CollectionNotSupportedError` (all
        # 409), and `ChargeDeclinedError` (402 -- Billing API Contract
        # Hardening, Phase 5: the provider actually attempted the charge and
        # declined it) are rendered centrally by
        # `common.exception_handlers.vinta_exception_handler` -- no local
        # try/except needed here.
        self.subscription_service.retry_payment(
            subscription,
            payment_token=data["payment_token"],
            idempotency_key=data["idempotency_key"],
        )

        # Re-fetched through the virtual-model-optimized queryset, same reason
        # as `change_plan` above -- and still GRACE/RESTRICTED here, since
        # recovery is webhook-driven; see the `200` response description.
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
        responses={
            201: SubscriptionAddOnSerializer,
            400: OpenApiResponse(
                response=BILLING_ERROR_BODY_SERIALIZER,
                description=(
                    "The resource's current plan limit carries no `overage_unit_price`, so "
                    "it has no catalog-derived price to purchase as an add-on "
                    '(`code: "add_on_not_purchasable"`, `AddOnNotPurchasableError`).'
                ),
            ),
            409: OpenApiResponse(
                response=BILLING_ERROR_BODY_SERIALIZER,
                description=(
                    "The provider this organization resolves to is not configured in this "
                    "deployment, so the one-time charge cannot be driven "
                    '(`code: "payment_provider_not_configured"`, '
                    "`PaymentProviderNotConfiguredError`)."
                ),
            ),
        },
    )
    def create(self, request, *args, **kwargs):
        organization = _require_organization(request)
        billing_root = resolve_billing_root(organization)
        # See `SubscriptionViewSet.get_subscription`'s comment: `has_permission`
        # cannot know *which* organization this write is for, since the write
        # acts on the billing root rather than on the organization the request
        # resolved -- `has_object_permission` is the real gate, run here against
        # that root.
        self.check_object_permissions(request, billing_root)
        subscription = self._get_subscription(organization)

        request_serializer = AddOnPurchaseRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        data = request_serializer.validated_data

        # `AddOnNotPurchasableError` (400) is rendered centrally by
        # `common.exception_handlers.vinta_exception_handler` -- no local
        # try/except needed here.
        add_on = self.subscription_service.purchase_add_on(
            subscription,
            data["resource_key"],
            data["quantity"],
            data["is_recurring"],
            data["idempotency_key"],
            data.get("payment_token", ""),
        )

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
