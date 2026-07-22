import logging
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
from rest_framework.viewsets import GenericViewSet, ViewSet

from common.utils.view_utils import TenantScopedViewMixin
from organizations.permissions import IsOrganizationAdmin
from payments.constants import PaymentStatuses
from payments.exceptions import ProviderWebhookEventIdMissingError, UnknownPaymentProviderError
from payments.models import BillingProfile, SubscriptionAddOn
from payments.models import PaymentStatusUpdate as PaymentStatusUpdateModel
from payments.serializers import BillingProfileSerializer
from payments.services.dunning_service import FAILED_SUBSCRIPTION_PAYMENT_STATUSES


if TYPE_CHECKING:
    from payments.services.dunning_service import DunningService
    from payments.services.payment_service import PaymentService
    from payments.services.subscription_service import SubscriptionService


logger = logging.getLogger(__name__)


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

        - **Approved**: grants the capacity for whichever plan the subscription
          is currently on (``SubscriptionService.confirm_plan_change``), records
          a confirmed payment method, and -- **first** -- resolves any
          GRACE/RESTRICTED dunning state back to ACTIVE
          (``DunningService.resolve_payment_success``). Runs on every approved
          charge, not only the first one after an upgrade or a dunning retry;
          every call here is idempotent, so a routine renewal simply re-affirms
          state that was already correct. ``resolve_payment_success`` runs
          *before* ``confirm_plan_change`` so the latter's own (idempotent)
          ``billing_state`` write is a same-state no-op by the time it runs --
          the two never disagree about which write actually happened.
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
            self.dunning_service.resolve_payment_success(subscription)
            self.subscription_service.confirm_plan_change(subscription)
            self.subscription_service.record_payment_method(
                subscription.organization, subscription.payment_provider, subscription.external_id
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
