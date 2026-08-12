import logging
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Annotated

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404

from dependency_injector.wiring import Provide, inject
from django_virtual_models.generic_views import GenericVirtualModelViewMixin
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet, ViewSet

from common.utils.view_utils import TenantScopedViewMixin
from payments.billing_views import _require_organization
from payments.constants import PaymentStatuses
from payments.exceptions import (
    PaymentProviderNotConfiguredError,
    ProviderWebhookEventIdMissingError,
    UnknownPaymentProviderError,
)
from payments.models import BillingProfile, SubscriptionAddOn
from payments.models import PaymentStatusUpdate as PaymentStatusUpdateModel
from payments.serializers import BillingProfileSerializer, PaymentProviderSerializer
from payments.services.dunning_service import FAILED_SUBSCRIPTION_PAYMENT_STATUSES
from payments.services.provider_credentials import resolve_public_credentials
from tenancy.permissions import IsOrganizationAdmin


if TYPE_CHECKING:
    from payments.services.dunning_service import DunningService
    from payments.services.payment_provider_resolver import PaymentProviderResolver
    from payments.services.payment_service import PaymentService
    from payments.services.subscription_service import SubscriptionService


logger = logging.getLogger(__name__)


def _coerce_payment_value(value: object) -> Decimal:
    """Best-effort ``Decimal`` coercion for ``Payment.value``, defensive
    against an untyped provider payload -- not a claim about any one
    provider's actual shape. ``payment`` here is the exact in-memory instance
    ``PaymentService.receive_subscription_payment_update`` just built via
    ``PaymentModel.objects.create(...)``, so Django's ``DecimalField``
    coercion (which only runs on save/load from the database) has not
    happened yet, and an adapter could in principle hand back ``value`` as a
    raw JSON number, string, or something malformed.

    Fails **closed**: ``Decimal(None)`` raises ``TypeError`` and
    ``Decimal("")`` raises ``InvalidOperation`` -- either would otherwise 500
    the webhook, and a provider retries a failed delivery forever, so an
    unparseable value must never crash this path. Treating it as ``0`` instead
    routes it through the ordinary "nothing collected yet" branch: dunning is
    never resolved on an amount this code could not actually parse.
    """
    try:
        return Decimal(value)  # type: ignore[arg-type]
    except (TypeError, InvalidOperation):
        logger.error(
            "Could not coerce subscription payment value to Decimal; treating as 0. value=%r",
            value,
        )
        return Decimal(0)


class PaymentsViewSet(ViewSet):
    """Inbound provider webhooks.

    These are called by the payment provider, not by a logged-in user of this
    app — there is no session/JWT to authenticate against, so DRF's default
    authentication/permission stack is explicitly disabled here. Authenticity is
    instead established per-request via the provider's own signature scheme
    (``PaymentService.verify_payment_webhook_signature`` /
    ``verify_subscription_webhook_signature``), and every verified delivery is
    recorded in ``ProviderWebhookEvent`` so a provider redelivery of the same event
    (at-least-once delivery is standard for webhooks) is only ever processed once.
    """

    authentication_classes = ()
    permission_classes = (AllowAny,)
    #: Unauthenticated + each delivery triggers an outbound provider API call
    #: (`check_status`/`get_payment_payload`) — bound abuse with a generous
    #: per-IP rate rather than leaving these fully unthrottled. Provider retry
    #: volume for a single event is low, so this should never affect legitimate
    #: deliveries.
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "payment-webhook"

    @inject
    def __init__(
        self,
        *args,
        payment_service: Annotated["PaymentService", Provide["payment_service"]],
        subscription_service: Annotated["SubscriptionService", Provide["subscription_service"]],
        dunning_service: Annotated["DunningService", Provide["dunning_service"]],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.payment_service = payment_service
        self.subscription_service = subscription_service
        self.dunning_service = dunning_service

    @extend_schema(
        summary="Receive payment updates",
        description="This endpoint is used to receive payment updates from a payment provider.",
        request=None,
        responses={
            200: {"description": "Payment update received."},
            400: {"description": "Malformed payload."},
            403: {"description": "Invalid or missing signature."},
            404: {"description": "Unknown payment provider."},
        },
    )
    @action(
        methods=["post"],
        detail=True,
        # add the provider to the URL path
        # `detail=True`'s `pk` is a correlation aid only (matches the payment id in
        # the `notification_url` we hand MercadoPago at payment-creation time) — it
        # is not used to authenticate or look up anything here. Do not change the
        # route: it is already baked into every `notification_url` sent so far.
        url_path="payment-update/<str:provider>",
        url_name="payment-update",
    )
    def payment_update(self, request, *args, **kwargs):
        """
        Handle payment updates.
        """
        provider = kwargs.get("provider", "")

        # `request.body` must be captured before `request.data` — Django raises
        # `RawPostDataException` if the raw stream was already consumed by DRF's
        # parser, and the signature must be checked against the literal bytes the
        # provider sent, not a re-serialization of the parsed payload.
        raw_body = request.body
        headers = dict(request.headers)

        try:
            signature_valid = self.payment_service.verify_payment_webhook_signature(
                provider, raw_body, headers
            )
        except UnknownPaymentProviderError:
            return Response(
                {"detail": f"Unknown payment provider: {provider!r}."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not signature_valid:
            logger.warning("Rejected payment webhook with invalid signature: provider=%s", provider)
            return Response({"detail": "Invalid signature."}, status=status.HTTP_403_FORBIDDEN)

        try:
            status_update = self.payment_service.handle_payment_webhook(
                provider, raw_body, headers, request.data
            )
        except ProviderWebhookEventIdMissingError:
            return Response(
                {"detail": "Payload is missing the notification id."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if status_update is not None:
            self._apply_confirmed_payment_side_effects(status_update)

        return Response({"message": "Payment update received."})

    def _apply_confirmed_payment_side_effects(
        self, status_update: PaymentStatusUpdateModel
    ) -> None:
        """A one-time payment (``payment_update`` webhook) confirmed
        ``APPROVED`` is what grants an add-on's capacity and records a
        confirmed payment method -- never the request that merely *initiates*
        the purchase. See ``SubscriptionService.purchase_add_on`` /
        ``activate_add_on`` / ``record_payment_method`` for the reasoning; this
        is the one place both are connected to a real webhook delivery.
        """
        if status_update.status != PaymentStatuses.APPROVED:
            return
        payment = status_update.payment
        organization = payment.organization
        if organization is not None:
            self.subscription_service.record_payment_method(
                organization, payment.payment_provider, payment.external_id
            )
        add_on = SubscriptionAddOn.objects.filter(payment=payment).first()
        if add_on is not None:
            self.subscription_service.activate_add_on(add_on)

    @extend_schema(
        summary="Receive subscription payment updates",
        description=(
            "This endpoint is used to receive subscription payment updates from a payment provider."
        ),
        request=None,
        responses={
            200: {"description": "Subscription payment update received."},
            400: {"description": "Malformed payload."},
            403: {"description": "Invalid or missing signature."},
            404: {"description": "Unknown payment provider."},
        },
    )
    @action(
        methods=["post"],
        detail=True,
        url_path="subscription-payment-update/<str:provider>",
        url_name="subscription-payment-update",
    )
    def subscription_payment_update(self, request, *args, **kwargs):
        """
        Handle subscription payment updates.
        """
        provider = kwargs.get("provider", "")

        # See the comment in `payment_update` — order matters here too.
        raw_body = request.body
        headers = dict(request.headers)

        try:
            signature_valid = self.payment_service.verify_subscription_webhook_signature(
                provider, raw_body, headers
            )
        except UnknownPaymentProviderError:
            return Response(
                {"detail": f"Unknown payment provider: {provider!r}."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not signature_valid:
            logger.warning(
                "Rejected subscription payment webhook with invalid signature: provider=%s",
                provider,
            )
            return Response({"detail": "Invalid signature."}, status=status.HTTP_403_FORBIDDEN)

        try:
            status_update = self.payment_service.handle_subscription_payment_webhook(
                provider, raw_body, headers, request.data
            )
        except ProviderWebhookEventIdMissingError:
            return Response(
                {"detail": "Payload is missing the notification id."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if status_update is not None:
            self._apply_subscription_payment_side_effects(status_update)

        return Response({"message": "Subscription payment update received."})

    def _apply_subscription_payment_side_effects(
        self, status_update: PaymentStatusUpdateModel
    ) -> None:
        """React to a subscription charge's outcome.

        - **Approved, and the payment actually collected money**: grants the
          capacity for whichever plan the subscription is currently on
          (``SubscriptionService.confirm_plan_change``), records a confirmed
          payment method, and -- **first** -- resolves any GRACE/RESTRICTED
          dunning state back to ACTIVE (``DunningService.resolve_payment_success``).
          Runs on every approved, non-zero charge, not only the first one after
          an upgrade or a dunning retry; every call here is idempotent, so a
          routine renewal simply re-affirms state that was already correct.
          ``resolve_payment_success`` runs *before* ``confirm_plan_change`` so
          the latter's own (idempotent) ``billing_state`` write is a same-state
          no-op by the time it runs -- the two never disagree about which write
          actually happened.
        - **Approved, but $0 collected** (Billing API Contract Hardening,
          Phase 4's zero-amount guard): neither ``resolve_payment_success``,
          ``confirm_plan_change``, nor ``record_payment_method`` runs. This is
          **defense in depth against a provider that does emit** a $0
          approved subscription-payment update (e.g. two offsetting proration
          line items netting to zero) -- not a claim that Stripe itself
          currently routes one here: as of BLOCKER 1's fix, a genuinely $0
          Stripe invoice has no PaymentIntent, so `receive_payment_update`
          resolves to `None` before this method is ever called for it. The
          guard stays regardless, because a $0 approved status is not proof
          the payer's outstanding balance was collected on any provider that
          *does* reach this method with one, and treating it as recovery
          would flip a GRACE/RESTRICTED subscription to ACTIVE while the real
          balance stays unpaid forever (the exact false-recovery this phase's
          Stripe probe caught -- see `SubscriptionService.retry_payment`'s
          docstring for the numbers). **``confirm_plan_change`` must be
          skipped too, not only ``resolve_payment_success``**: `confirm_plan_change`
          drives its *own* unconditional ``transition_billing_state(..., ACTIVE)``
          call, documented on that method as "a same-state no-op" only under
          the assumption that `resolve_payment_success` already moved GRACE/
          RESTRICTED to ACTIVE first -- an assumption this guard deliberately
          breaks, so calling `confirm_plan_change` anyway would silently
          re-open the exact hole this guard exists to close.
          **``record_payment_method`` is skipped too**: a $0 approved payment
          is not proof the instrument is actually chargeable, and this call
          grants `has_payment_method` (which gates overage accrual) and
          permanently pins `BillingProfile.payment_provider` -- the same
          reasoning the amount guard above already applies, just not
          previously applied to this call too.
        - **Failed** (``FAILED_SUBSCRIPTION_PAYMENT_STATUSES``): moves the
          subscription into GRACE (``DunningService.enter_grace``) -- the
          dunning ladder owns everything from here (retry schedule, escalating
          notification, eventual RESTRICTED on expiry). Never touches
          ``PaymentMethod`` -- see ``DunningService``'s module docstring.
        - Anything else (``PENDING``, ``IN_PROCESS``, ...) is not yet a final
          outcome; no side effect fires until a later delivery resolves it.
        """
        payment = status_update.payment
        subscription = payment.subscription
        if subscription is None:
            return
        if status_update.status == PaymentStatuses.APPROVED:
            # `payment.value` is coerced through `_coerce_payment_value` rather
            # than compared directly: `payment` here is the exact in-memory
            # instance `PaymentService.receive_subscription_payment_update`
            # just built via `PaymentModel.objects.create(...)`, so Django's
            # `DecimalField` coercion (save/load from the database) has not
            # run yet, and this is defensive against any adapter handing back
            # an untyped value -- not a claim about one specific provider's
            # actual shape. Coercing (rather than comparing the raw value)
            # also fails closed: an absent or unparseable value becomes `0`,
            # never a 500 that would otherwise leave dunning unresolved and
            # have the provider retry the delivery forever.
            if _coerce_payment_value(payment.value) > 0:
                self.dunning_service.resolve_payment_success(subscription)
                self.subscription_service.confirm_plan_change(subscription)
                self.subscription_service.record_payment_method(
                    subscription.organization,
                    subscription.payment_provider,
                    subscription.external_id,
                )
        elif status_update.status in FAILED_SUBSCRIPTION_PAYMENT_STATUSES:
            self.dunning_service.enter_grace(subscription)


class BillingProfileViewSet(
    TenantScopedViewMixin,
    GenericVirtualModelViewMixin,
    GenericViewSet,
):
    serializer_class = BillingProfileSerializer
    queryset = BillingProfile.objects.all()
    lookup_url_kwarg = "pk"
    lookup_field = "pk"
    permission_classes = (IsAuthenticated,)

    #: Writes touch the organization's tax document number and payer identity, not
    #: just "my own" data, so they are gated to org admins. Reads stay open to any
    #: authenticated member (IsAuthenticated, above).
    write_actions = (
        "create_billing_profile",
        "update_billing_profile",
        "partial_update_billing_profile",
    )

    def get_permissions(self):
        if self.action in self.write_actions:
            return [IsAuthenticated(), IsOrganizationAdmin()]
        return super().get_permissions()

    def get_queryset(self) -> QuerySet[BillingProfile]:
        # Chain the organization filter on top of the virtual-model-optimized base
        # queryset (GenericVirtualModelViewMixin.get_queryset()) rather than
        # constructing a fresh queryset, so scoping doesn't undo the serializer's
        # select_related/prefetch optimization.
        queryset = super().get_queryset()
        organization = self.request.organization  # type: ignore[attr-defined]
        if organization is None:
            return queryset.none()
        return queryset.filter(organization=organization)

    def get_billing_profile(self):
        organization = self.request.organization  # type: ignore[attr-defined]
        organization_pk = organization.pk if organization is not None else None
        return get_object_or_404(self.get_queryset(), pk=organization_pk)

    @extend_schema(
        summary="Retrieve billing profile",
        description="Retrieve the billing profile of the active organization.",
        responses={200: BillingProfileSerializer},
    )
    @action(
        methods=["get"],
        detail=False,
        url_path="",
        url_name="retrieve",
    )
    def retrieve_billing_profile(self, request, *args, **kwargs):
        billing_profile = self.get_billing_profile()

        serializer = self.get_serializer(billing_profile)
        return Response(serializer.data)

    @extend_schema(
        summary="Create billing profile",
        description="Create a new billing profile for the active organization.",
        responses={201: BillingProfileSerializer},
    )
    @action(
        methods=["post"],
        detail=False,
        url_path="",
        url_name="create",
    )
    def create_billing_profile(self, request, *args, **kwargs):
        if self.get_queryset().exists():
            return Response(
                {"detail": "A billing profile already exists for this organization."},
                status=status.HTTP_409_CONFLICT,
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        billing_profile = serializer.save()

        return Response(
            self.get_serializer(billing_profile).data,
            status=201,
        )

    @extend_schema(
        summary="Update billing profile",
        description="Update the billing profile of the active organization.",
        responses={200: BillingProfileSerializer},
    )
    @action(
        methods=["put"],
        detail=False,
        url_path="",
        url_name="update",
    )
    def update_billing_profile(self, request, *args, **kwargs):
        billing_profile = self.get_billing_profile()
        serializer = self.get_serializer(billing_profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        billing_profile = serializer.save()

        return Response(self.get_serializer(billing_profile).data)

    @extend_schema(
        summary="Partially update billing profile",
        description="Partially update the billing profile of the active organization.",
        responses={200: BillingProfileSerializer},
    )
    @action(
        methods=["patch"],
        detail=False,
        url_path="",
        url_name="partial_update",
    )
    def partial_update_billing_profile(self, request, *args, **kwargs):
        billing_profile = self.get_billing_profile()
        serializer = self.get_serializer(billing_profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        billing_profile = serializer.save()

        return Response(self.get_serializer(billing_profile).data)


class PaymentProviderViewSet(TenantScopedViewMixin, ViewSet):
    """``GET /billing/payment-provider/`` -- the active organization's payment provider
    (its pin when set, ``settings.DEFAULT_PAYMENT_PROVIDER`` otherwise -- resolved through
    ``PaymentProviderResolver``, the one place both this endpoint and Phase 4's charge
    routing implement that rule) plus that provider's browser-safe public credentials.

    Split from the unauthenticated system-default endpoint (see
    ``DefaultPaymentProviderView`` below): the two have different auth, throttle, and
    cardinality semantics, and previously sharing one ``ViewSet`` required hand-rolling
    DRF's authenticator resolution (``get_authenticators`` /
    ``_action_for_current_request``) so ``authentication_classes = ()`` applied to only
    one action.

    Mounted directly via ``path()`` in ``payments/routes.py``'s ``extra_patterns``
    (``PaymentProviderViewSet.as_view({"get": "retrieve_provider"})``), bypassing the
    shared DRF router, so the bare ``/billing/payment-provider/`` path does not depend on
    the router's static list-route -- which always binds ``GET`` to an action literally
    named ``list`` (``rest_framework.routers.SimpleRouter.routes``). That fixed binding
    would force ``self.action == "list"`` regardless of the Python method name, which
    makes drf-spectacular's ``AutoSchema._is_list_view()`` document this endpoint as an
    array (``type: array, items: $ref PaymentProvider``) even with the explicit
    ``responses={200: PaymentProviderSerializer}`` override below, since
    ``_is_list_view()`` checks ``view.action == "list"`` before consulting the override.
    Binding the action name as ``retrieve_provider`` instead means ``self.action`` is
    never ``"list"``, so the override is honored and the schema documents a single
    object, matching the actual response.
    """

    serializer_class = PaymentProviderSerializer
    permission_classes = (IsAuthenticated,)

    @inject
    def __init__(
        self,
        *args,
        payment_provider_resolver: Annotated[
            "PaymentProviderResolver", Provide["payment_provider_resolver"]
        ],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.payment_provider_resolver = payment_provider_resolver

    @extend_schema(
        summary="Get the active organization's payment provider",
        description=(
            "Returns the payment provider the active organization is pinned to (or the "
            "system default when unpinned) plus its browser-safe public credentials."
        ),
        responses={
            200: PaymentProviderSerializer,
            403: {"description": "No active organization."},
            409: {
                "description": (
                    "The resolved provider is unknown or has no public credentials "
                    "configured in this deployment."
                )
            },
        },
    )
    def retrieve_provider(self, request, *args, **kwargs):
        """``GET /billing/payment-provider/``. See the class docstring for why this is
        not named ``list``."""
        organization = _require_organization(request)
        provider = self.payment_provider_resolver.resolve_for_organization(organization)
        try:
            credentials = resolve_public_credentials(provider)
        except PaymentProviderNotConfiguredError:
            return Response(
                {
                    "detail": (
                        f"Payment provider {provider!r} is not configured in this deployment."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(PaymentProviderSerializer(credentials).data)


class DefaultPaymentProviderView(APIView):
    """``GET /billing/payment-provider/default/`` -- the system default provider's public
    credentials, unauthenticated (mirrors ``PaymentsViewSet``'s pattern above): a frontend
    needs this to render *a* payment form before, or entirely without, a session.

    A standalone ``APIView`` rather than a shared action on ``PaymentProviderViewSet`` --
    see that class's docstring for why the two are split. ``authentication_classes`` /
    ``permission_classes`` / ``throttle_classes`` are set once here, at class level, with
    no per-action switching.
    """

    serializer_class = PaymentProviderSerializer
    authentication_classes = ()
    permission_classes = (AllowAny,)
    #: Unauthenticated -- bound with the shared ``payment-provider`` scope
    #: (``settings.DEFAULT_THROTTLE_RATES``).
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "payment-provider"

    @inject
    def __init__(
        self,
        *args,
        payment_provider_resolver: Annotated[
            "PaymentProviderResolver", Provide["payment_provider_resolver"]
        ],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.payment_provider_resolver = payment_provider_resolver

    @extend_schema(
        summary="Get the system default payment provider",
        description=(
            "Unauthenticated -- the system default provider's browser-safe public "
            "credentials, so a frontend can render a payment form before (or without) a "
            "session."
        ),
        responses={
            200: PaymentProviderSerializer,
            503: {"description": "The default provider has no public credentials configured."},
            429: {"description": "Throttled."},
        },
    )
    def get(self, request, *args, **kwargs):
        provider = self.payment_provider_resolver.resolve_default()
        try:
            credentials = resolve_public_credentials(provider)
        except PaymentProviderNotConfiguredError:
            return Response(
                {
                    "detail": (
                        f"Payment provider {provider!r} is not configured in this deployment."
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(PaymentProviderSerializer(credentials).data)
