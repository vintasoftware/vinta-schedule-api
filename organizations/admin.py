import logging
from typing import Annotated, Any

from django import forms
from django.contrib import admin
from django.http import HttpRequest

from dependency_injector.wiring import Provide, inject

from organizations.models import Organization, OrganizationBranding
from payments.services.subscription_service import SubscriptionService


logger = logging.getLogger(__name__)


class OrganizationAdminForm(forms.ModelForm):
    """Rejects a ``parent`` selection that would create a cycle in the
    organization tree.

    ``parent`` is freely editable in admin with no other acyclicity check, which
    is how a cycle like the one ``resolve_billing_root``'s cycle guard exists for
    gets created in the first place (Phase 4 review BLOCKER 4/3). Left editable
    rather than made read-only after creation: reparenting an organization
    between resellers is a legitimate admin operation with no other UI path, and
    this validation — not immutability — is what protects the invariant.
    """

    class Meta:
        model = Organization
        fields = (
            "name",
            "parent",
            "should_sync_rooms",
            "external_event_update_policy",
            "can_invite_organizations",
        )

    def clean(self) -> dict[str, Any] | None:
        cleaned_data = super().clean()
        parent = None if cleaned_data is None else cleaned_data.get("parent")
        if parent is not None and self.instance.pk is not None:
            seen: set[int] = set()
            org: Organization | None = parent
            while org is not None:
                if org.pk == self.instance.pk:
                    raise forms.ValidationError(
                        {
                            "parent": (
                                "This would create a cycle in the organization tree: "
                                f"organization {self.instance.pk} is already an ancestor "
                                f"of the selected parent."
                            )
                        }
                    )
                if org.pk in seen:
                    # A pre-existing cycle elsewhere in the tree, unrelated to this
                    # edit — nothing more to learn by continuing the walk.
                    break
                seen.add(org.pk)
                org = org.parent
        return cleaned_data


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    """Admin interface for Organization.

    Exposes can_invite_organizations as the ONLY place it can be toggled.
    Also exposes external_event_update_policy for managing event edit/delete policies.

    A fourth organization-creation path alongside the REST funnel, signup, and the
    reseller GraphQL mutation (Phase 4 review BLOCKER 4) — ``save_model`` places a
    newly created organization on the default billing plan the same way
    ``OrganizationService.create_organization`` does, so an org created here is
    never left plan-less.
    """

    form = OrganizationAdminForm
    list_display = (
        "id",
        "name",
        "can_invite_organizations",
        "external_event_update_policy",
        "parent",
        "created",
        "modified",
    )
    list_filter = (
        "can_invite_organizations",
        "external_event_update_policy",
        "created",
        "modified",
    )
    search_fields = ("name", "id")
    ordering = ("-created",)
    readonly_fields = ("created", "modified", "id")

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "id",
                    "name",
                    "parent",
                    "should_sync_rooms",
                    "external_event_update_policy",
                    "created",
                    "modified",
                )
            },
        ),
        (
            "Reseller Capability",
            {
                "fields": ("can_invite_organizations",),
                "description": (
                    "Enable this organization to invite and create child organizations. "
                    "This is the ONLY place this setting can be toggled. "
                    "When enabled, the organization gains the full reseller capability bundle."
                ),
            },
        ),
    )

    @inject
    def save_model(
        self,
        request: HttpRequest,
        obj: Organization,
        form: forms.ModelForm,
        change: bool,
        subscription_service: Annotated[
            SubscriptionService | None, Provide["subscription_service"]
        ] = None,
    ) -> None:
        """Persist ``obj`` and, for a newly created organization, place it on the
        default billing plan — every organization always has exactly one active
        plan, from creation; there is no plan-less state.

        No-op for edits to an existing organization, and (via
        ``create_subscription_for_organization`` itself) for a reseller child
        that pools against its billing root's subscription instead.
        """
        super().save_model(request, obj, form, change)
        if change:
            return
        if subscription_service is None:
            logger.error(
                "OrganizationAdmin.save_model: subscription_service not injected "
                "(DI not wired?) — organization %s created with no Subscription.",
                obj.pk,
            )
            return
        subscription_service.create_subscription_for_organization(obj)


@admin.register(OrganizationBranding)
class OrganizationBrandingAdmin(admin.ModelAdmin):
    """Admin interface for OrganizationBranding."""

    list_display = ("id", "organization", "app_name", "support_email", "created_at", "updated_at")
    list_filter = ("created_at", "updated_at")
    search_fields = ("organization__name", "app_name", "support_email")
    readonly_fields = ("created_at", "updated_at", "id")
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "id",
                    "organization",
                    "app_name",
                    "logo_url",
                    "primary_color",
                    "secondary_color",
                    "support_email",
                    "return_url_allowlist",
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )
