from django.contrib import admin

from payments.models import (
    BillingAddress,
    BillingPlan,
    BillingProfile,
    Payment,
    PaymentStatusUpdate,
    PlanEntitlement,
    PlanLimit,
    ProviderWebhookEvent,
    Refund,
    RefundStatusUpdate,
    Subscription,
    SubscriptionPlanLimit,
    SubscriptionStatusUpdate,
)


class PlanLimitInline(admin.TabularInline):
    model = PlanLimit
    extra = 0
    fields = ("resource_key", "limit_value", "kind", "overage_unit_price")


class PlanEntitlementInline(admin.TabularInline):
    model = PlanEntitlement
    extra = 0
    fields = ("entitlement_key", "is_enabled")


@admin.register(BillingPlan)
class BillingPlanAdmin(admin.ModelAdmin):
    """Admin interface for the catalog ``BillingPlan``, with its ``PlanLimit`` /
    ``PlanEntitlement`` rows editable inline."""

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
    inlines = (PlanLimitInline, PlanEntitlementInline)


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


class SubscriptionPlanLimitInline(admin.TabularInline):
    """Editable per-subscription limit copy — the support lever for a stuck
    organization. Any row an admin touches here is stamped ``is_overridden=True``
    on save (see ``SubscriptionAdmin.save_formset``) so it survives the next plan
    change untouched, which is what makes this the intended enforcement bypass
    instead of a code-level one."""

    model = SubscriptionPlanLimit
    extra = 0
    fields = ("resource_key", "limit_value", "kind", "overage_unit_price", "is_overridden")
    readonly_fields = ("is_overridden",)


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
    inlines = (SubscriptionPlanLimitInline,)

    def save_formset(self, request, form, formset, change):
        """`SubscriptionPlanLimit` rows an admin creates or edits here are
        hand-edited by definition — mark them `is_overridden=True` so a later plan
        change (`SubscriptionService.change_plan`) leaves them untouched. Rows the
        admin merely viewed without changing are not returned by
        `formset.save(commit=False)` and are left alone."""
        if formset.model is SubscriptionPlanLimit:
            instances = formset.save(commit=False)
            for instance in instances:
                instance.is_overridden = True
                instance.save()
            for obj in formset.deleted_objects:
                obj.delete()
            formset.save_m2m()
        else:
            super().save_formset(request, form, formset, change)


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
