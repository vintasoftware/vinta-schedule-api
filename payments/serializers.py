import django_virtual_models as v
from rest_framework import serializers

from payments.models import BillingAddress, BillingProfile, Subscription
from payments.virtual_models import (
    BillingAddressVirtualModel,
    BillingProfileVirtualModel,
    SubscriptionVirtualModel,
)


class SubscriptionSerializer(v.VirtualModelSerializer):
    """
    Serializer for Subscription virtual model.
    """

    class Meta:
        model = Subscription
        virtual_model = SubscriptionVirtualModel
        fields = (
            "id",
            "status",
            "start_date",
            "end_date",
        )
        read_only_fields = (
            "id",
            "status",
            "start_date",
            "end_date",
        )


class BillingAddressSerializer(v.VirtualModelSerializer):
    """
    Serializer for BillingAddress virtual model.
    """

    class Meta:
        model = BillingAddress
        virtual_model = BillingAddressVirtualModel
        fields = (
            "id",
            "street_name",
            "street_number",
            "neighborhood",
            "address_line_2",
            "city",
            "state",
            "country",
            "zip_code",
        )
        read_only_fields = ("id",)


class BillingProfileSerializer(v.VirtualModelSerializer):
    """
    Serializer for BillingProfile virtual model.
    """

    id = serializers.IntegerField(source="user_id", read_only=True)  # noqa: A003
    billing_address = BillingAddressSerializer()

    class Meta:
        model = BillingProfile
        virtual_model = BillingProfileVirtualModel
        fields = (
            "id",
            "document_type",
            "document_number",
            "billing_address",
            "created",
            "modified",
        )
        read_only_fields = (
            "id",
            "created",
            "modified",
        )

    def create(self, validated_data):
        """
        Create a new BillingProfile and its related BillingAddress.
        """
        billing_address_data = validated_data.pop("billing_address")
        billing_address = BillingAddress.objects.create(**billing_address_data)
        billing_profile = BillingProfile.objects.create(
            user=self.context["request"].user, billing_address=billing_address, **validated_data
        )
        return billing_profile

    def update(self, instance, validated_data):
        """
        Update an existing BillingProfile and its related BillingAddress.
        """
        billing_address_data = validated_data.pop("billing_address", None)
        if billing_address_data:
            for attr, value in billing_address_data.items():
                setattr(instance.billing_address, attr, value)
            instance.billing_address.save()

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance
