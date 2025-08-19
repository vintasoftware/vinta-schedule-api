from typing import TYPE_CHECKING

from django.core.exceptions import ObjectDoesNotExist
from django.db import models

from common.models import BaseModel
from payments.constants import (
    PaymentProviders,
    PaymentStatuses,
    RefundStatuses,
    SubscriptionStatuses,
)
from users.models import User


if TYPE_CHECKING:
    from django_stubs_ext.db.models.manager import RelatedManager

    from organizations.models import SubscriptionPlan


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
        return (
            f"{self.id} {self.user} - {self.city} - {self.state} - {self.country} - {self.zip_code}"
        )

    @property
    def user(self):
        return getattr(self, "billing_profile", None) and self.billing_profile.user


class BillingProfile(BaseModel):
    user = models.OneToOneField(
        User, primary_key=True, on_delete=models.CASCADE, related_name="billing_profile"
    )
    document_type = models.CharField(max_length=50)
    document_number = models.CharField(max_length=50)
    billing_address = models.OneToOneField(
        BillingAddress, on_delete=models.CASCADE, related_name="billing_profile"
    )

    def __str__(self):
        return f"{self.pk} {self.user} - {self.document_type} - {self.document_number}"


class Subscription(BaseModel):
    status = models.CharField(
        max_length=50, choices=SubscriptionStatuses, default=SubscriptionStatuses.PENDING_SEND
    )
    external_id = models.CharField(max_length=255, blank=True)
    billing_profile = models.ForeignKey(
        BillingProfile, on_delete=models.CASCADE, related_name="subscriptions"
    )
    start_date = models.DateField()
    end_date = models.DateField()

    membership: "SubscriptionPlan"

    def __str__(self):
        return f"{self.id} - {self.status} - {self.start_date} - {self.end_date}"

    @property
    def plan(self):
        try:
            return self.membership.tier
        except ObjectDoesNotExist:
            return None


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
        return f"{self.id} {self.user} - {self.value} - {self.payment_provider} - {self.status} - {self.created.isoformat()}"

    @property
    def user(self):
        return getattr(self, "billing_profile", None) and self.billing_profile.user


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
    status = models.CharField(max_length=50, choices=PaymentStatuses)
    description = models.TextField(blank=True)
    external_id = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.id} {self.payment} - {self.status} - {self.created.isoformat()}"
