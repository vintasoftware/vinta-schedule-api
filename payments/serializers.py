from typing import TYPE_CHECKING

import django_virtual_models as v
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from payments.billing_constants import BillingInterval, LimitedResource
from payments.constants import PaymentProviders
from payments.models import (
    BillingAddress,
    BillingPeriodResourceUsage,
    BillingPeriodSummary,
    BillingPlan,
    BillingProfile,
    MeteredOccurrence,
    PlanEntitlement,
    PlanLimit,
    Subscription,
    SubscriptionAddOn,
)
from payments.services.provider_credentials import PublicProviderCredentials
from payments.virtual_models import (
    BillingAddressVirtualModel,
    BillingPlanVirtualModel,
    BillingProfileVirtualModel,
    SubscriptionVirtualModel,
)


if TYPE_CHECKING:
    from calendar_integration.models import CalendarEvent


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

    ``payment_token`` is required in practice the *first* time a billing root
    ever attaches a payment instrument -- there is otherwise no provider-facing
    card/token to create the provider-side subscription against. Optional here
    (blank by default) because it is only actually required when
    ``Subscription.external_id`` is still blank; see
    ``SubscriptionService._initiate_upgrade`` for the exact condition and
    ``PaymentTokenRequiredError`` for the 400 a caller gets if it omits the
    token when one was needed.
    """

    plan_slug = serializers.SlugField()
    billing_interval = serializers.ChoiceField(
        choices=BillingInterval.choices, default=BillingInterval.MONTHLY
    )
    idempotency_key = serializers.CharField(max_length=255)
    payment_token = serializers.CharField(max_length=255, required=False, allow_blank=True)


class AddOnPurchaseRequestSerializer(serializers.Serializer):
    """Body of ``POST /billing/add-ons/``. See ``ChangePlanRequestSerializer``
    for why ``payment_token`` is required in practice despite being optional
    here -- an add-on purchase is a one-time charge and needs an instrument to
    charge, exactly like a first-ever plan upgrade does."""

    resource_key = serializers.ChoiceField(choices=LimitedResource.choices)
    quantity = serializers.IntegerField(min_value=1)
    is_recurring = serializers.BooleanField(default=True)
    idempotency_key = serializers.CharField(max_length=255)
    payment_token = serializers.CharField(max_length=255, required=False, allow_blank=True)


class UsageByOrganizationSerializer(serializers.Serializer):
    """One organization's contribution to a pooled ``GET /billing/usage/`` figure.

    Sourced from ``EntitlementService.get_usage_breakdown`` / the ``usage_breakdown_for_root``
    entry point it shares with ``CycleCloseService``. An organization in the pool
    that contributed **nothing** to this resource is **omitted from the list
    entirely** -- never present with ``usage: 0`` -- matching that breakdown's
    absent-not-zero contract.
    """

    organization_id = serializers.IntegerField(
        help_text="pk of the contributing organization, within the caller's pooled billing subtree."
    )
    name = serializers.CharField(help_text="The contributing organization's name.")
    usage = serializers.IntegerField(help_text="This organization's share of the resource's usage.")


class BillingPlanSnapshotSerializer(serializers.Serializer):
    """The plan in force for the current billing cycle, as reported by ``GET
    /billing/usage/``. ``null`` when the caller's billing root has no
    ``Subscription`` (``billing_state: "free"``)."""

    slug = serializers.CharField(help_text="The billing plan's slug.")
    name = serializers.CharField(help_text="The billing plan's display name.")
    currency = serializers.CharField(help_text='The billing plan\'s currency, e.g. "USD".')


class BillingPeriodBoundsSerializer(serializers.Serializer):
    """The ``[start, end)`` bounds of the cycle in progress right now, resolved
    through the same anchor (``resolve_billing_period`` /
    ``current_billing_period_start``) the meter and the usage counters use --
    never read off ``Subscription.current_period_start`` directly, which goes
    stale the moment one cycle elapses. ``null`` when there is no subscription.
    """

    start = serializers.DateTimeField(help_text="Inclusive start of the current billing period.")
    end = serializers.DateTimeField(help_text="Exclusive end of the current billing period.")


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
    included_in_plan = serializers.IntegerField(
        allow_null=True,
        help_text=(
            "The plan-only portion of limit_value (SubscriptionPlanLimit.limit_value), "
            "before any add-on capacity. null under the same fail-open rule limit_value "
            "follows: no subscription, no plan-limit row for this resource, or an "
            "explicitly unlimited row."
        ),
    )
    add_on_quantity = serializers.IntegerField(
        help_text=(
            "The sum of every active SubscriptionAddOn's quantity for this resource. "
            "included_in_plan + add_on_quantity == limit_value whenever limit_value is "
            "non-null -- these two fields decompose limit_value, they do not redefine it."
        )
    )
    by_organization = UsageByOrganizationSerializer(
        many=True,
        help_text=(
            "Per-organization attribution of current_usage across the caller's pooled "
            "billing subtree. An organization that contributed nothing is omitted, "
            "never present with usage: 0. Ordered by organization_id ascending."
        ),
    )


class UsageResponseSerializer(serializers.Serializer):
    billing_state = serializers.CharField()
    billing_root_organization_id = serializers.IntegerField(
        help_text="pk of the billing root this response was resolved against."
    )
    plan = BillingPlanSnapshotSerializer(
        allow_null=True,
        help_text="The plan in force this cycle. null when there is no subscription.",
    )
    billing_period = BillingPeriodBoundsSerializer(
        allow_null=True,
        help_text="Bounds of the cycle in progress now. null when there is no subscription.",
    )
    estimated_overage_total = serializers.DecimalField(
        max_digits=12,
        decimal_places=4,
        help_text=(
            "Overage money accrued so far in the current, open billing period -- "
            "MeteredOccurrenceQuerySet.overage_total() over the caller's pooled subtree. "
            'Accrued-to-date, never a projection of the whole cycle. "0.0000" when there '
            "is no subscription."
        ),
    )
    limits = EffectiveLimitUsageSerializer(many=True)


class BillingPeriodResourceUsageSerializer(serializers.ModelSerializer):
    """One resource's snapshot as of a closed billing period
    (``BillingPeriodResourceUsage``), nested under ``GET
    /billing/usage/periods/{id}/``'s ``resources`` key.

    ``reconciliation_unmetered``/``reconciliation_orphaned`` live on the parent
    ``BillingPeriodSummary`` row, not here, and neither is serialized anywhere
    in this module -- internal investigation data, surfaced only in Django
    admin (see the plan's Non-goals).
    """

    resource_key = serializers.CharField(
        help_text="The LimitedResource member this row reports on."
    )
    kind = serializers.CharField(
        allow_null=True,
        help_text=(
            "prepaid or postpaid, as classified at close time. null when this "
            "resource's limit could not be resolved at close."
        ),
    )
    total = serializers.IntegerField(
        allow_null=True,
        help_text=(
            "This resource's usage as counted at close, summed across the pooled "
            "subtree. null means the count was **not recorded** -- a period that "
            "closed before this feature shipped, or a LimitedResource member added "
            "after this period closed -- and must never be displayed as 0. A "
            "recorded usage of zero serializes as the integer 0, distinct from null."
        ),
    )
    limit_value = serializers.IntegerField(
        allow_null=True,
        help_text=(
            "The effective ceiling in force at close time. null means "
            "**unlimited** -- a different null than total's; a client must not "
            "collapse the two into the same meaning."
        ),
    )
    overage_unit_price = serializers.DecimalField(
        max_digits=10,
        decimal_places=4,
        allow_null=True,
        help_text=(
            "Price per unit of overage. For event_occurrences with at least one "
            "overage row this period, stamped from those MeteredOccurrence rows "
            "rather than the live effective limit, so a later price change cannot "
            "make this row disagree with overage_total. null for a prepaid "
            "resource, or when no single stamped price applies to this period."
        ),
    )
    by_organization = UsageByOrganizationSerializer(
        many=True,
        source="_by_organization_rows",
        help_text=(
            "Per-organization contribution to total, across the pooled subtree at "
            "close time. An organization that contributed nothing is omitted, "
            "never present with usage: 0. Ordered by organization_id ascending -- "
            "the identical shape GET /billing/usage/'s by_organization uses. "
            "Names are resolved at **read time** against the organizations table, "
            "so they reflect each organization's current name, not its name as of "
            "when this period closed -- this row has no name snapshot. An "
            'organization that no longer exists renders with name: "" rather '
            "than being dropped from the list; its count still counts toward total."
        ),
    )

    class Meta:
        model = BillingPeriodResourceUsage
        fields = (
            "resource_key",
            "kind",
            "total",
            "limit_value",
            "overage_unit_price",
            "by_organization",
        )
        read_only_fields = fields

    def to_representation(self, instance):
        """Build the ``UsageByOrganizationSerializer``-shaped rows from the
        model's persisted ``{str(organization_id): count}`` blob plus the
        ``organization_names`` map the view resolves once per request and
        threads through ``context`` -- see ``BillingPeriodViewSet.retrieve``.
        """
        organization_names: dict[int, str] = self.context.get("organization_names", {})
        instance._by_organization_rows = [
            {
                "organization_id": organization_id,
                "name": organization_names.get(organization_id, ""),
                "usage": usage,
            }
            for organization_id, usage in sorted(
                (int(pk), usage) for pk, usage in instance.by_organization.items()
            )
        ]
        return super().to_representation(instance)


class BillingPeriodSummarySerializer(serializers.ModelSerializer):
    """One closed billing period, as a durable statement -- one row of ``GET
    /billing/usage/periods/``. See ``BillingPeriodSummaryDetailSerializer`` for
    the ``resources`` breakdown the detail action adds.

    ``reconciliation_unmetered``/``reconciliation_orphaned`` are deliberately
    absent from every field below -- internal investigation data, surfaced only
    in Django admin (see the plan's Non-goals).
    """

    id = serializers.IntegerField(help_text="pk of this statement.")  # noqa: A003
    billing_period_start = serializers.DateTimeField(
        help_text="Inclusive start of the closed period."
    )
    billing_period_end = serializers.DateTimeField(help_text="Exclusive end of the closed period.")
    plan_slug = serializers.CharField(
        help_text=(
            "The billing plan in force for this period, snapshotted at close time "
            "-- a later plan change does not rewrite this."
        )
    )
    plan_name = serializers.CharField(
        help_text="Display name of the plan in force for this period."
    )
    billing_interval = serializers.CharField(
        help_text="The subscription's billing interval for this period."
    )
    currency = serializers.CharField(help_text='The plan\'s currency for this period, e.g. "USD".')
    overage_total = serializers.DecimalField(
        max_digits=12, decimal_places=4, help_text="Overage money charged for this period."
    )
    charged = serializers.BooleanField(
        help_text="Whether an overage charge was actually made for this period."
    )
    payment_id = serializers.IntegerField(
        allow_null=True,
        help_text=(
            "pk of the Payment that settled this period's overage. null when charged is false."
        ),
    )
    closed_at = serializers.DateTimeField(help_text="When CycleCloseService wrote this statement.")

    class Meta:
        model = BillingPeriodSummary
        # Explicitly widened to `tuple[str, ...]` (rather than the narrower
        # fixed-length literal type mypy would otherwise infer) so
        # `BillingPeriodSummaryDetailSerializer.Meta` can extend it with
        # `"resources"` without a tuple-length mismatch.
        fields: tuple[str, ...] = (
            "id",
            "billing_period_start",
            "billing_period_end",
            "plan_slug",
            "plan_name",
            "billing_interval",
            "currency",
            "overage_total",
            "charged",
            "payment_id",
            "closed_at",
        )
        read_only_fields = fields


class BillingPeriodSummaryDetailSerializer(BillingPeriodSummarySerializer):
    """``GET /billing/usage/periods/{id}/`` -- one statement's full detail,
    adding its per-resource breakdown to every field
    ``BillingPeriodSummarySerializer`` already reports."""

    resources = BillingPeriodResourceUsageSerializer(
        many=True,
        read_only=True,
        help_text=(
            "Every LimitedResource member recorded for this period. Prefetched, "
            "so retrieving one statement is a bounded number of queries "
            "regardless of how many resources exist."
        ),
    )

    class Meta(BillingPeriodSummarySerializer.Meta):
        fields = (*BillingPeriodSummarySerializer.Meta.fields, "resources")
        read_only_fields = fields


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


class StripePublicCredentialsSerializer(serializers.Serializer):
    """The browser-safe half of Stripe's credentials -- never the secret API key."""

    publishable_key = serializers.CharField()


class MercadoPagoPublicCredentialsSerializer(serializers.Serializer):
    """The browser-safe half of MercadoPago's credentials -- never the secret access token."""

    public_key = serializers.CharField()


class PaymentProviderSerializer(serializers.Serializer):
    """Response shape for ``GET /billing/payment-provider/`` and its unauthenticated
    ``/default/`` sibling: the resolved provider slug plus that provider's public
    credentials. Only the object matching ``provider`` is populated; the other is
    ``null``, so a client never receives keys for a provider it did not resolve to.

    Plain ``serializers.Serializer`` -- nothing here is DB-backed. Serializes a
    ``payments.services.provider_credentials.PublicProviderCredentials`` instance, not a
    model, hence the explicit ``to_representation`` rather than attribute-matching nested
    serializers (the dataclass's flat ``stripe_publishable_key``/``mercadopago_public_key``
    fields don't line up 1:1 with this serializer's nested ``stripe``/``mercadopago``
    objects).
    """

    provider = serializers.ChoiceField(choices=PaymentProviders.choices)
    stripe = StripePublicCredentialsSerializer(allow_null=True)
    mercadopago = MercadoPagoPublicCredentialsSerializer(allow_null=True)

    def to_representation(self, instance: PublicProviderCredentials) -> dict:
        stripe = None
        if instance.stripe_publishable_key is not None:
            stripe = StripePublicCredentialsSerializer(
                {"publishable_key": instance.stripe_publishable_key}
            ).data
        mercadopago = None
        if instance.mercadopago_public_key is not None:
            mercadopago = MercadoPagoPublicCredentialsSerializer(
                {"public_key": instance.mercadopago_public_key}
            ).data
        return {
            "provider": instance.provider,
            "stripe": stripe,
            "mercadopago": mercadopago,
        }


class MeteredOccurrenceOrganizationSerializer(serializers.Serializer):
    """The organization a ledger row is attributed to -- ``GET
    /billing/usage/occurrences/``'s ``organization`` field. Names are batch
    resolved by the view (``MeteredOccurrenceViewSet``) once per page, the
    same pattern ``UsageByOrganizationSerializer`` uses."""

    id = serializers.IntegerField(help_text="pk of the attributed organization.")  # noqa: A003
    name = serializers.CharField(help_text="The attributed organization's name.")


class LedgerCalendarSerializer(serializers.Serializer):
    """The calendar a ledger row's event lives on -- nested under
    ``LedgerEventSerializer``."""

    id = serializers.IntegerField(help_text="pk of the calendar.")  # noqa: A003
    name = serializers.CharField(help_text="The calendar's display name.")


class LedgerEventOwnerSerializer(serializers.Serializer):
    """One owner of a ledger row's event's calendar (``CalendarOwnership``).
    ``CalendarEvent`` has no organizer field of its own -- ownership is
    calendar-level, and a calendar can have several owners."""

    user_id = serializers.IntegerField(help_text="pk of the owning membership's user.")
    name = serializers.CharField(help_text="The owning user's display name.")


class LedgerEventSerializer(serializers.Serializer):
    """The event a ``GET /billing/usage/occurrences/`` row was metered
    against. Resolved by the view in one batched query per page --
    ``MeteredOccurrence.event_id`` is a soft reference (``BigIntegerField``,
    not a ``ForeignKey``) precisely so the billing record outlives the event
    (see the ``MeteredOccurrence`` model docstring); this can never become a
    per-row lookup.
    """

    id = serializers.IntegerField(help_text="pk of the event.")  # noqa: A003
    title = serializers.CharField(
        help_text=(
            "The series root's title, not the individual occurrence's own. "
            "event_id stores the series root -- following bulk_modification_parent "
            "back through any splits -- so a modified occurrence's row shows the "
            "master's title rather than its own current one."
        )
    )
    calendar = LedgerCalendarSerializer(
        allow_null=True, help_text="The calendar this event lives on."
    )
    owners = LedgerEventOwnerSerializer(
        many=True,
        help_text=(
            "Owners of the event's calendar (CalendarOwnership). CalendarEvent "
            "carries no organizer field of its own -- ownership is calendar-level, "
            "and a calendar can have several owners."
        ),
    )


class MeteredOccurrenceSerializer(serializers.ModelSerializer):
    """One row of ``GET /billing/usage/occurrences/`` -- the post-paid ledger
    behind an overage charge, so a customer disputing an invoice can tie every
    unit of money to a specific occurrence.

    ``event``/``organization`` are not model relations on ``MeteredOccurrence``
    (``event_id`` is a soft reference; ``organization`` has no name of its own
    here) -- both are built in ``to_representation`` from maps the view
    resolves once per page and threads through ``context`` (``event_map``,
    ``organization_names``), never a per-row query.
    """

    organization = MeteredOccurrenceOrganizationSerializer(
        source="_organization_row",
        help_text="The organization this occurrence is attributed to.",
    )
    event = LedgerEventSerializer(
        source="_event_row",
        allow_null=True,
        help_text=(
            "null when the referenced event no longer exists -- an expected state "
            "(a MeteredOccurrence outlives its event by design, see the model "
            "docstring), not an error. The charge still stands; unit_price is "
            "unaffected either way."
        ),
    )

    class Meta:
        model = MeteredOccurrence
        fields = (
            "id",
            "organization",
            "event",
            "occurrence_start",
            "billing_period_start",
            "is_within_allowance",
            "unit_price",
        )
        read_only_fields = fields

    def to_representation(self, instance: MeteredOccurrence):
        organization_names: dict[int, str] = self.context.get("organization_names", {})
        event_map: dict[int, CalendarEvent] = self.context.get("event_map", {})

        instance._organization_row = {  # type: ignore[attr-defined]
            "id": instance.organization_id,
            "name": organization_names.get(instance.organization_id, ""),
        }

        event = event_map.get(instance.event_id)
        event_row = None
        if event is not None:
            calendar = event.calendar
            owners = (
                [
                    {
                        "user_id": ownership.membership_user_id,
                        "name": ownership.membership.user.get_full_name(),
                    }
                    for ownership in calendar.ownerships.all()
                    if ownership.membership_user_id is not None
                ]
                if calendar is not None
                else []
            )
            event_row = {
                "id": event.pk,
                "title": event.title,
                "calendar": (
                    {"id": calendar.pk, "name": calendar.name} if calendar is not None else None
                ),
                "owners": owners,
            }
        instance._event_row = event_row  # type: ignore[attr-defined]

        return super().to_representation(instance)
