from typing import TYPE_CHECKING, ClassVar

from django.db import models
from django.db.models import Q, UniqueConstraint

from common.models import BaseModel
from payments.billing_constants import BillingInterval, BillingState
from payments.constants import (
    PaymentProviders,
    PaymentStatuses,
    RefundStatuses,
    SubscriptionStatuses,
)


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

    This is a minimal forward reference: only the fields needed for
    ``Subscription.plan`` to resolve to a real model exist here. The full
    catalog — ``PlanLimit``, ``PlanEntitlement``, plan seeding, and admin
    inlines — lands in a later phase (plan catalog, limits, and entitlements).
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

    class Meta:
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["is_default_for_new_organizations"],
                condition=Q(is_default_for_new_organizations=True),
                name="uniq_default_billing_plan",
            )
        ]

    def __str__(self):
        return self.name


class BillingProfile(BaseModel):
    organization = models.OneToOneField(
        "organizations.Organization",
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="billing_profile",
    )
    document_type = models.CharField(max_length=50)
    document_number = models.CharField(max_length=50)
    billing_address = models.OneToOneField(
        BillingAddress, on_delete=models.CASCADE, related_name="billing_profile"
    )

    def __str__(self):
        return f"{self.pk} {self.organization} - {self.document_type} - {self.document_number}"


class Subscription(BaseModel):
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
