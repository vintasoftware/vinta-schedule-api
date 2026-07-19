from typing import TYPE_CHECKING, ClassVar

from django.db import models
from django.db.models import Q, UniqueConstraint

from common.models import BaseModel
from payments.billing_constants import (
    BillingInterval,
    BillingState,
    Entitlement,
    LimitedResource,
    LimitKind,
    ProviderWebhookRoute,
)
from payments.constants import (
    PaymentProviders,
    PaymentStatuses,
    RefundStatuses,
    SubscriptionStatuses,
)
from payments.managers import ProviderWebhookEventManager


if TYPE_CHECKING:
    from django_stubs_ext.db.models.manager import RelatedManager


class BillingAddress(BaseModel):
    street_name = models.TextField()
    street_number = models.TextField()
    neighborhood = models.TextField(blank=True)
    address_line_2 = models.TextField(blank=True)
    city = models.CharField(max_length=255)
    state = models.CharField(max_length=255)
    country = models.CharField(max_length=255)
    zip_code = models.CharField(max_length=10)

    billing_profile: "BillingProfile"

    def __str__(self):
        return f"{self.id} {self.organization} - {self.city} - {self.state} - {self.country} - {self.zip_code}"

    @property
    def organization(self):
        return getattr(self, "billing_profile", None) and self.billing_profile.organization


class BillingPlan(BaseModel):
    """Catalog plan that a ``Subscription`` is sold against.

    Carries its ``PlanLimit`` / ``PlanEntitlement`` rows (the plan catalog proper).
    There is no feature flag for the limits/entitlements rollout: the ``unlimited``
    plan — every ``PlanLimit.limit_value`` NULL, every ``PlanEntitlement`` enabled —
    *is* the kill switch. Catalog edits here never propagate to an already-sold
    subscription; see ``SubscriptionPlanLimit`` (a later phase) for the
    per-subscription copy.
    """

    slug = models.SlugField(max_length=100, unique=True)
    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True, db_index=True)
    is_default_for_new_organizations = models.BooleanField(default=False)
    monthly_price = models.DecimalField(max_digits=10, decimal_places=2)
    annual_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3)
    grace_period_days = models.PositiveIntegerField(null=True, blank=True)

    subscriptions: "RelatedManager[Subscription]"
    limits: "RelatedManager[PlanLimit]"
    entitlements: "RelatedManager[PlanEntitlement]"

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["is_default_for_new_organizations"],
                condition=Q(is_default_for_new_organizations=True),
                name="uniq_default_billing_plan",
            )
        ]

    def __str__(self):
        return self.name


class PlanLimit(BaseModel):
    """A single resource ceiling on a ``BillingPlan``.

    ``limit_value=NULL`` means no ceiling (unlimited) — never treat NULL as zero.
    ``kind`` mirrors ``LimitedResource``'s own prepaid/postpaid split so an
    effective-limit resolution does not have to cross-reference the choices class.
    """

    plan = models.ForeignKey(BillingPlan, on_delete=models.CASCADE, related_name="limits")
    resource_key = models.CharField(max_length=100, choices=LimitedResource)
    limit_value = models.PositiveIntegerField(null=True, blank=True)
    kind = models.CharField(max_length=20, choices=LimitKind)
    overage_unit_price = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["plan", "resource_key"],
                name="uniq_plan_limit_resource",
            )
        ]

    def __str__(self):
        return f"{self.plan} - {self.resource_key} - {self.limit_value}"


class PlanEntitlement(BaseModel):
    """A single boolean feature gate on a ``BillingPlan``."""

    plan = models.ForeignKey(BillingPlan, on_delete=models.CASCADE, related_name="entitlements")
    entitlement_key = models.CharField(max_length=100, choices=Entitlement)
    is_enabled = models.BooleanField(default=False)

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["plan", "entitlement_key"],
                name="uniq_plan_entitlement_key",
            )
        ]

    def __str__(self):
        return f"{self.plan} - {self.entitlement_key} - {self.is_enabled}"


class BillingProfile(BaseModel):
    organization = models.OneToOneField(
        "organizations.Organization",
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="billing_profile",
    )
    # Payer identity sent to the payment gateway. Distinct from the future
    # `OrganizationMembership.is_billing_owner` (Phase 9), which is about who may
    # *manage* billing — these fields are about what the gateway needs to charge
    # the organization (e.g. MercadoPago rejects a payer with no email).
    contact_first_name = models.CharField(max_length=255)
    contact_last_name = models.CharField(max_length=255, blank=True)
    contact_email = models.EmailField()
    contact_phone = models.CharField(max_length=50, blank=True)
    document_type = models.CharField(max_length=50)
    document_number = models.CharField(max_length=50)
    billing_address = models.OneToOneField(
        BillingAddress, on_delete=models.CASCADE, related_name="billing_profile"
    )

    def __str__(self):
        return f"{self.pk} {self.organization} - {self.document_type} - {self.document_number}"


class Subscription(BaseModel):
    """An organization's subscription to a ``BillingPlan``.

    Two status concepts coexist here and share member names (``active``,
    ``cancelled``, ``pending``) — do not conflate them:

    - ``status`` (``SubscriptionStatuses``) mirrors the provider-reported state of
      the subscription, fed by ``SubscriptionStatusUpdate`` rows as the gateway
      reports them (e.g. MercadoPago's ``authorized`` / ``paused`` / ``cancelled``).
    - ``billing_state`` (``BillingState``) is this app's internal billing
      lifecycle (free / active / grace / restricted / cancelled) used to gate
      access. It is derived from, but not identical to, ``status``.
    """

    organization = models.OneToOneField(
        "organizations.Organization", on_delete=models.CASCADE, related_name="subscription"
    )
    plan = models.ForeignKey(BillingPlan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(
        max_length=50, choices=SubscriptionStatuses, default=SubscriptionStatuses.PENDING_SEND
    )
    billing_state = models.CharField(
        max_length=20, choices=BillingState, default=BillingState.FREE, db_index=True
    )
    billing_interval = models.CharField(
        max_length=10, choices=BillingInterval, default=BillingInterval.MONTHLY
    )
    current_period_start = models.DateTimeField()
    current_period_end = models.DateTimeField(db_index=True)
    grace_period_ends_at = models.DateTimeField(null=True, blank=True, db_index=True)
    external_id = models.CharField(max_length=255, blank=True, db_index=True)
    plan_external_id = models.CharField(max_length=255, blank=True)
    payment_provider = models.CharField(max_length=50, choices=PaymentProviders)

    def __str__(self):
        return (
            f"{self.id} - {self.status} - {self.current_period_start} - {self.current_period_end}"
        )


class Payment(BaseModel):
    value = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=50)
    payment_provider = models.CharField(max_length=50, choices=PaymentProviders)
    external_id = models.CharField(max_length=255)
    status = models.CharField(max_length=50, choices=PaymentStatuses)
    original_status = models.CharField(max_length=50)
    billing_profile = models.ForeignKey(
        BillingProfile, on_delete=models.CASCADE, related_name="payments"
    )
    payment_method = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="payments", null=True, blank=True
    )

    status_updates: "RelatedManager[PaymentStatusUpdate]"

    def __str__(self):
        return f"{self.id} {self.organization} - {self.value} - {self.payment_provider} - {self.status} - {self.created.isoformat()}"

    @property
    def organization(self):
        return getattr(self, "billing_profile", None) and self.billing_profile.organization


class Refund(BaseModel):
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="refunds")
    value = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=50)
    external_id = models.CharField(max_length=255)
    status = models.CharField(
        max_length=50, choices=RefundStatuses, default=RefundStatuses.PENDING_SEND
    )

    def __str__(self):
        return f"{self.id} {self.payment} - {self.value} - {self.currency} - {self.created.isoformat()}"


class PaymentStatusUpdate(BaseModel):
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="status_updates")
    status = models.CharField(max_length=50, choices=PaymentStatuses)
    description = models.TextField(blank=True)
    external_id = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.id} {self.payment} - {self.status} - {self.created.isoformat()}"


class SubscriptionStatusUpdate(BaseModel):
    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="status_updates"
    )
    status = models.CharField(max_length=50, choices=SubscriptionStatuses)
    description = models.TextField(blank=True)
    external_id = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.id} {self.subscription} - {self.status} - {self.created.isoformat()}"


class RefundStatusUpdate(BaseModel):
    refund = models.ForeignKey(Refund, on_delete=models.CASCADE, related_name="status_updates")
    status = models.CharField(max_length=50, choices=RefundStatuses)
    description = models.TextField(blank=True)
    external_id = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.id} {self.refund} - {self.status} - {self.created.isoformat()}"


class ProviderWebhookEvent(BaseModel):
    """Idempotency ledger for inbound payment-provider webhook notifications.

    Not tenant-scoped: a webhook notification arrives before we know which
    organization it resolves to (see the billing plans and limits plan's Data Model
    Changes — cross-organization billing reads are the reason these models stay
    plain-FK rather than ``OrganizationModel``). ``(provider, route,
    external_event_id)`` uniquely identifies one delivery attempt at the provider;
    ``processed_at`` is set only once the corresponding domain update
    (payment/subscription status) has actually been applied, so a row that exists
    with ``processed_at=None`` means a previous delivery was recorded but crashed
    before finishing — the next delivery for the same event is allowed to retry
    rather than being silently dropped.
    """

    provider = models.CharField(max_length=50, choices=PaymentProviders)
    route = models.CharField(max_length=50, choices=ProviderWebhookRoute)
    external_event_id = models.CharField(max_length=255)
    payload = models.JSONField(default=dict, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects: ClassVar[ProviderWebhookEventManager] = ProviderWebhookEventManager()

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["provider", "route", "external_event_id"],
                name="uniq_provider_webhook_event",
            )
        ]

    def __str__(self):
        return f"{self.provider} - {self.route} - {self.external_event_id}"
