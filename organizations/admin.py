from django.contrib import admin

from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationTier,
)


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    """Admin interface for Organization.

    Exposes can_invite_organizations as the ONLY place it can be toggled.
    """

    list_display = ("id", "name", "can_invite_organizations", "parent", "created", "modified")
    list_filter = ("can_invite_organizations", "created", "modified")
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
                    "tier",
                    "should_sync_rooms",
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

    def get_readonly_fields(self, request, obj=None):
        """Make all fields read-only except can_invite_organizations and tier."""
        if obj:  # editing existing
            return [*self.readonly_fields, "name", "parent", "should_sync_rooms"]
        return self.readonly_fields


@admin.register(OrganizationTier)
class OrganizationTierAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "created", "modified")
    ordering = ("name",)


@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "organization", "role", "is_active", "created", "modified")
    list_filter = ("role", "is_active", "created")
    search_fields = ("user__email", "organization__name")
    ordering = ("-created",)
    readonly_fields = ("created", "modified", "id")

    fieldsets = (
        (
            None,
            {"fields": ("id", "user", "organization", "role", "is_active", "created", "modified")},
        ),
    )


@admin.register(OrganizationInvitation)
class OrganizationInvitationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "email",
        "organization",
        "invited_by",
        "accepted_at",
        "expires_at",
        "created",
    )
    list_filter = ("accepted_at", "expires_at", "created")
    search_fields = ("email", "organization__name")
    ordering = ("-created",)
    readonly_fields = ("created", "modified", "id", "token_hash")

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "id",
                    "email",
                    "first_name",
                    "last_name",
                    "organization",
                    "invited_by",
                    "created",
                    "modified",
                )
            },
        ),
        (
            "Invitation State",
            {
                "fields": (
                    "token_hash",
                    "expires_at",
                    "accepted_at",
                    "membership",
                ),
                "classes": ("collapse",),
            },
        ),
    )
