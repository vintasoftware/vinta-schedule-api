from typing import Any

from django.contrib import admin
from django.forms.models import BaseInlineFormSet
from django.http import HttpRequest

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
    SubscriptionAddOn,
    SubscriptionEntitlement,
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
    organization. A row an admin edits here is stamped ``is_overridden=True`` on
    save (see ``SubscriptionAdmin.save_formset``) so it survives the next plan
    change untouched, which is what makes this the intended enforcement bypass
    instead of a code-level one.

    ``is_overridden`` itself is editable (not in ``readonly_fields``): a support
    grant is meant to be temporary, and the way to clear it — put the row back
    under normal plan-change control — is to uncheck it here. ``save_formset``
    special-cases this: a save where ``is_overridden`` is the *only* field that
    changed is not re-stamped ``True``, so unchecking the box actually clears it.
    Any other edit (changing the limit value, kind, etc.) is still stamped
    ``True``, since touching a row's data is itself the act of overriding it.
    """

    model = SubscriptionPlanLimit
    extra = 0
    fields = ("resource_key", "limit_value", "kind", "overage_unit_price", "is_overridden")


class SubscriptionEntitlementInline(admin.TabularInline):
    """Editable per-subscription entitlement copy — same support-lever semantics
    as ``SubscriptionPlanLimitInline``. Without this, a stuck entitlement (e.g. an
    org that needs ``PARTNER_API`` enabled ahead of a plan change) had no support
    lever at all, unlike limits."""

    model = SubscriptionEntitlement
    extra = 0
    fields = ("entitlement_key", "is_enabled", "is_overridden")


class SubscriptionAddOnInline(admin.TabularInline):
    """Purchased extra capacity, visible alongside the limits it modifies.

    Read-only: an add-on represents money that changed hands, and
    ``purchase_idempotency_key`` is what ties it to that transaction. Granting
    capacity by hand belongs in ``SubscriptionPlanLimitInline`` (which stamps
    ``is_overridden``), not here — creating an add-on row in admin would fabricate
    a purchase with no payment behind it.
    """

    model = SubscriptionAddOn
    extra = 0
    # `max_num = 0` renders no add form at all — the inline is a read-only ledger
    # view, not a creation surface.
    max_num = 0
    fields = ("resource_key", "quantity", "is_recurring", "is_active", "external_id")
    readonly_fields = fields
    can_delete = False


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
    inlines = (SubscriptionPlanLimitInline, SubscriptionEntitlementInline, SubscriptionAddOnInline)

    def save_formset(
        self, request: HttpRequest, form: Any, formset: BaseInlineFormSet, change: bool
    ) -> None:
        """`SubscriptionPlanLimit` / `SubscriptionEntitlement` rows an admin
        creates or edits here are hand-edited by definition — mark them
        `is_overridden=True` so a later plan change (`SubscriptionService.change_plan`)
        leaves them untouched. Rows the admin merely viewed without changing are
        not returned by `formset.save(commit=False)` and are left alone.

        Exception: a row whose *only* changed field is `is_overridden` itself is
        not re-stamped — it is left at whatever value the admin just set. Without
        this, unchecking the box to clear a support override would make
        `form.has_changed()` true, the row would still come back from
        `formset.save(commit=False)`, and the loop below would immediately
        re-stamp it `True`, making the override permanently one-way.
        """
        if formset.model in (SubscriptionPlanLimit, SubscriptionEntitlement):
            instances = formset.save(commit=False)
            override_only_pks = {
                obj.pk
                for obj, changed_fields in formset.changed_objects
                if changed_fields == ["is_overridden"]
            }
            for instance in instances:
                if instance.pk not in override_only_pks:
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
