from typing import TYPE_CHECKING, Annotated

from django.shortcuts import get_object_or_404

from dependency_injector.wiring import Provide, inject
from django_virtual_models.generic_views import GenericVirtualModelViewMixin
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet, ViewSet

from payments.models import BillingProfile
from payments.serializers import BillingProfileSerializer


if TYPE_CHECKING:
    from payments.services.payment_service import PaymentService


class PaymentsViewSet(ViewSet):
    @extend_schema(
        summary="Receive payment updates",
        description="This endpoint is used to receive payment updates from MercadoPago.",
        request=None,
        responses={200: {"description": "Payment update received."}},
    )
    @action(
        methods=["post"],
        detail=True,
        # add the provider to the URL path
        url_path="payment-update/<str:provider>",
        url_name="payment-update",
    )
    @inject
    def payment_update(
        self,
        request,
        payment_service: Annotated["PaymentService", Provide["payment_service"]],
    ):
        """
        Handle payment updates.
        """
        # This method should be implemented to handle payment updates
        payment_service.receive_payment_update(request.data)
        return Response({"message": "Payment update received."})

    @extend_schema(
        summary="Receive subscription payment updates",
        description="This endpoint is used to receive subscription payment updates from MercadoPago.",
        request=None,
        responses={200: {"description": "Subscription payment update received."}},
    )
    @action(
        methods=["post"],
        detail=True,
        url_path="subscription-payment-update/<str:provider>",
        url_name="subscription-payment-update",
    )
    @inject
    def subscription_payment_update(
        self,
        request,
        payment_service: Annotated["PaymentService", Provide["payment_service"]],
    ):
        """
        Handle payment updates.
        """
        # This method should be implemented to handle payment updates
        payment_service.receive_subscription_payment_update(request.data)
        return Response({"message": "Payment update received."})


class BillingProfileViewSet(
    GenericVirtualModelViewMixin,
    GenericViewSet,
):
    serializer_class = BillingProfileSerializer
    queryset = BillingProfile.objects.all()
    lookup_url_kwarg = "pk"
    lookup_field = "pk"
    permission_classes = (IsAuthenticated,)

    def get_billing_profile(self):
        return get_object_or_404(self.get_queryset(), pk=self.request.user.pk)

    @extend_schema(
        summary="Retrieve billing profile",
        description="Retrieve the billing profile of the authenticated user.",
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
        description="Create a new billing profile for the authenticated user.",
        responses={201: BillingProfileSerializer},
    )
    @action(
        methods=["post"],
        detail=False,
        url_path="",
        url_name="create",
    )
    def create_billing_profile(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        billing_profile = serializer.save()

        return Response(
            self.get_serializer(billing_profile).data,
            status=201,
        )

    @extend_schema(
        summary="Update billing profile",
        description="Update the billing profile of the authenticated user.",
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
        description="Partially update the billing profile of the authenticated user.",
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
