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
from rest_framework.viewsets import GenericViewSet, ViewSet

from common.utils.view_utils import TenantScopedViewMixin
from organizations.permissions import IsOrganizationAdmin
from payments.exceptions import ProviderWebhookEventIdMissingError, UnknownPaymentProviderError
from payments.models import BillingProfile
from payments.serializers import BillingProfileSerializer


if TYPE_CHECKING:
    from payments.services.payment_service import PaymentService


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

    @inject
    def __init__(
        self,
        *args,
        payment_service: Annotated["PaymentService", Provide["payment_service"]],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.payment_service = payment_service

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
            self.payment_service.handle_payment_webhook(provider, request.data)
        except ProviderWebhookEventIdMissingError:
            return Response(
                {"detail": "Payload is missing the notification id."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({"message": "Payment update received."})

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
            self.payment_service.handle_subscription_payment_webhook(provider, request.data)
        except ProviderWebhookEventIdMissingError:
            return Response(
                {"detail": "Payload is missing the notification id."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({"message": "Subscription payment update received."})


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
