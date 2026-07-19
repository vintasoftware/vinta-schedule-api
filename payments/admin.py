from django.contrib import admin

from payments.models import (
    BillingAddress,
    BillingPlan,
    BillingProfile,
    Payment,
    PaymentStatusUpdate,
    ProviderWebhookEvent,
    Refund,
    RefundStatusUpdate,
    Subscription,
    SubscriptionStatusUpdate,
)


@admin.register(BillingPlan)
class BillingPlanAdmin(admin.ModelAdmin):
    """Admin interface for the catalog ``BillingPlan``.

    Phase 1 only carries the plan's own fields; ``PlanLimit`` / ``PlanEntitlement``
    inlines and catalog seeding land in a later phase.
    """

    list_display = (
        "id",
        "slug",
        "name",
        "is_active",
        "is_default_for_new_organizations",
        "monthly_price",
        "currency",
    )
    list_filter = ("is_active", "is_default_for_new_organizations", "currency")
    search_fields = ("slug", "name")
    ordering = ("slug",)
    readonly_fields = ("created", "modified")


@admin.register(BillingAddress)
class BillingAddressAdmin(admin.ModelAdmin):
    list_display = ("id", "city", "state", "country", "zip_code")
    search_fields = ("city", "state", "country", "zip_code")
    readonly_fields = ("created", "modified")


@admin.register(BillingProfile)
class BillingProfileAdmin(admin.ModelAdmin):
    list_display = ("organization", "document_type", "document_number")
    search_fields = ("organization__name", "document_number")
    readonly_fields = ("created", "modified")


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "organization",
        "plan",
        "status",
        "billing_state",
        "billing_interval",
        "current_period_start",
        "current_period_end",
    )
    list_filter = ("status", "billing_state", "billing_interval", "payment_provider")
    search_fields = ("organization__name", "external_id")
    readonly_fields = ("created", "modified")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "billing_profile",
        "value",
        "currency",
        "payment_provider",
        "status",
    )
    list_filter = ("status", "payment_provider")
    search_fields = ("external_id", "billing_profile__organization__name")
    readonly_fields = ("created", "modified")


@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    list_display = ("id", "payment", "value", "currency", "status")
    list_filter = ("status",)
    search_fields = ("external_id",)
    readonly_fields = ("created", "modified")


@admin.register(PaymentStatusUpdate)
class PaymentStatusUpdateAdmin(admin.ModelAdmin):
    list_display = ("id", "payment", "status", "created")
    list_filter = ("status",)
    readonly_fields = ("created", "modified")


@admin.register(SubscriptionStatusUpdate)
class SubscriptionStatusUpdateAdmin(admin.ModelAdmin):
    list_display = ("id", "subscription", "status", "created")
    list_filter = ("status",)
    readonly_fields = ("created", "modified")


@admin.register(RefundStatusUpdate)
class RefundStatusUpdateAdmin(admin.ModelAdmin):
    list_display = ("id", "refund", "status", "created")
    list_filter = ("status",)
    readonly_fields = ("created", "modified")


@admin.register(ProviderWebhookEvent)
class ProviderWebhookEventAdmin(admin.ModelAdmin):
    """Read-only operational visibility into the webhook idempotency ledger."""

    list_display = ("id", "provider", "route", "external_event_id", "processed_at", "created")
    list_filter = ("provider", "route")
    search_fields = ("external_event_id",)
    readonly_fields = ("created", "modified", "provider", "route", "external_event_id", "payload")
