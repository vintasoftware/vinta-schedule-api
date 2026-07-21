import django_virtual_models as v
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from payments.billing_constants import BillingInterval, LimitedResource
from payments.models import (
    BillingAddress,
    BillingPlan,
    BillingProfile,
    PlanEntitlement,
    PlanLimit,
    Subscription,
    SubscriptionAddOn,
)
from payments.virtual_models import (
    BillingAddressVirtualModel,
    BillingPlanVirtualModel,
    BillingProfileVirtualModel,
    SubscriptionVirtualModel,
)


class PlanLimitSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlanLimit
        fields = ("resource_key", "limit_value", "kind", "overage_unit_price")
        read_only_fields = fields


class PlanEntitlementSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlanEntitlement
        fields = ("entitlement_key", "is_enabled")
        read_only_fields = fields


class BillingPlanSerializer(v.VirtualModelSerializer):
    """The catalog view behind ``GET /billing/plans/`` — every active plan with
    its limits and entitlements, so a client can render an upgrade picker
    without a second round trip per plan."""

    limits = PlanLimitSerializer(many=True, read_only=True)
    entitlements = PlanEntitlementSerializer(many=True, read_only=True)

    class Meta:
        model = BillingPlan
        virtual_model = BillingPlanVirtualModel
        fields = (
            "id",
            "slug",
            "name",
            "is_active",
            "is_default_for_new_organizations",
            "monthly_price",
            "annual_price",
            "currency",
            "grace_period_days",
            "limits",
            "entitlements",
        )
        read_only_fields = fields


class SubscriptionAddOnSerializer(serializers.ModelSerializer):
    class Meta:
        model = SubscriptionAddOn
        fields = (
            "id",
            "resource_key",
            "quantity",
            "is_recurring",
            "is_active",
            "external_id",
            "created",
        )
        read_only_fields = fields


class SubscriptionSerializer(v.VirtualModelSerializer):
    """
    Serializer for Subscription virtual model.
    """

    plan = BillingPlanSerializer(read_only=True)
    pending_plan_slug: serializers.SlugRelatedField = serializers.SlugRelatedField(
        source="pending_plan", slug_field="slug", read_only=True
    )
    add_ons = SubscriptionAddOnSerializer(many=True, read_only=True)

    class Meta:
        model = Subscription
        virtual_model = SubscriptionVirtualModel
        fields = (
            "id",
            "plan",
            "billing_state",
            "billing_interval",
            "payment_provider",
            "current_period_start",
            "current_period_end",
            "grace_period_ends_at",
            "pending_plan_slug",
            "pending_billing_interval",
            "pending_plan_effective_at",
            "add_ons",
        )
        read_only_fields = fields


class ChangePlanRequestSerializer(serializers.Serializer):
    """Body of ``POST /billing/subscription/change-plan/``.

    ``payment_token`` is not in the plan's documented request body (``API
    Design`` lists only ``plan_slug``/``billing_interval``/``idempotency_key``)
    but is required in practice the *first* time a billing root ever attaches a
    payment instrument -- there is otherwise no provider-facing card/token to
    create the provider-side subscription against. Optional here (blank by
    default) because it is only actually required when
    ``Subscription.external_id`` is still blank; see
    ``SubscriptionService._initiate_upgrade`` for the exact condition and
    ``PaymentTokenRequiredError`` for the 400 a caller gets if it omits the
    token when one was needed. Documented in the phase report as a deliberate
    deviation from the plan's literal request shape.
    """

    plan_slug = serializers.SlugField()
    billing_interval = serializers.ChoiceField(
        choices=BillingInterval.choices, default=BillingInterval.MONTHLY
    )
    idempotency_key = serializers.CharField(max_length=255)
    payment_token = serializers.CharField(max_length=255, required=False, allow_blank=True)


class AddOnPurchaseRequestSerializer(serializers.Serializer):
    """Body of ``POST /billing/add-ons/``. See ``ChangePlanRequestSerializer``
    for why ``payment_token`` is present despite not being in the plan's
    literal request shape -- an add-on purchase is a one-time charge and needs
    an instrument to charge, exactly like a first-ever plan upgrade does."""

    resource_key = serializers.ChoiceField(choices=LimitedResource.choices)
    quantity = serializers.IntegerField(min_value=1)
    is_recurring = serializers.BooleanField(default=True)
    idempotency_key = serializers.CharField(max_length=255)
    payment_token = serializers.CharField(max_length=255, required=False, allow_blank=True)


class EffectiveLimitUsageSerializer(serializers.Serializer):
    """One row of ``GET /billing/usage/`` -- an ``EffectiveLimit`` paired with the
    ``current_usage`` ``EntitlementService.check_limit`` would compare it
    against. Not a ``ModelSerializer``: the source is a dataclass plus a
    separately-fetched usage count, not one model instance."""

    resource_key = serializers.CharField()
    kind = serializers.CharField(allow_null=True)
    limit_value = serializers.IntegerField(allow_null=True)
    current_usage = serializers.IntegerField(allow_null=True)
    overage_unit_price = serializers.DecimalField(max_digits=10, decimal_places=4, allow_null=True)


class UsageResponseSerializer(serializers.Serializer):
    billing_state = serializers.CharField()
    limits = EffectiveLimitUsageSerializer(many=True)


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

    id = serializers.IntegerField(source="organization_id", read_only=True)  # noqa: A003
    billing_address = BillingAddressSerializer()

    class Meta:
        model = BillingProfile
        virtual_model = BillingProfileVirtualModel
        fields = (
            "id",
            "contact_first_name",
            "contact_last_name",
            "contact_email",
            "contact_phone",
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
        organization = self.context["request"].organization
        if organization is None:
            raise PermissionDenied(
                "An active organization is required to create a billing profile."
            )

        billing_address_data = validated_data.pop("billing_address")
        billing_address = BillingAddress.objects.create(**billing_address_data)
        billing_profile = BillingProfile.objects.create(
            organization=organization,
            billing_address=billing_address,
            **validated_data,
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
