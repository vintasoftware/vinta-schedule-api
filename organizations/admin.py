from django.contrib import admin

from organizations.models import Organization, OrganizationBranding


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    """Admin interface for Organization.

    Exposes can_invite_organizations as the ONLY place it can be toggled.
    Also exposes external_event_update_policy for managing event edit/delete policies.
    """

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
