from typing import Annotated

from django import forms
from django.contrib import (
    admin,
    messages,
)
from django.db.models import QuerySet
from django.http import HttpRequest

from dependency_injector.wiring import Provide, inject

from public_api.models import ResourceAccess, SystemUser
from public_api.services import PublicAPIAuthService


class ResourceAccessInline(admin.TabularInline):
    """
    Inline admin for managing resource access associated with a system user.
    This allows admins to manage which resources the system user has access to.
    """

    model = ResourceAccess
    extra = 0
    verbose_name = "Resource Access"
    verbose_name_plural = "Resource Accesses"


@admin.register(SystemUser)
class SystemUserAdmin(admin.ModelAdmin):
    list_display = ("organization", "integration_name", "is_active", "created", "modified")
    search_fields = ("integration_name", "organization__name")
    list_filter = ("organization", "created", "modified")
    readonly_fields = ("long_lived_token_hash", "created", "modified", "is_active")
    fieldsets = (
        (
            None,
            {"fields": ("organization", "integration_name", "long_lived_token_hash", "is_active")},
        ),
        ("Timestamps", {"fields": ("created", "modified"), "classes": ("collapse",)}),
    )
    actions = ("deactivate_system_user",)
    inlines = (ResourceAccessInline,)

    def deactivate_system_user(self, request: HttpRequest, queryset: QuerySet[SystemUser]) -> None:
        """
        Custom action to deactivate selected system users.
        """
        updated_count = queryset.update(is_active=False)
        self.message_user(request, f"{updated_count} system user(s) deactivated.")

    @inject
    def __init__(
        self,
        *args,
        public_api_auth_service: Annotated[
            PublicAPIAuthService | None, Provide["public_api_auth_service"]
        ] = None,
        **kwargs,
    ):
        self.public_api_auth_service = public_api_auth_service
        super().__init__(*args, **kwargs)

    def save_model(
        self,
        request: HttpRequest,
        obj: SystemUser,
        form: forms.Form,
        change: bool,
    ) -> None:
        if not self.public_api_auth_service:
            raise ValueError("PublicAPIAuthService is not provided")

        if change:
            return super().save_model(request, obj, form, change)

        instance, token = self.public_api_auth_service.create_system_user(
            integration_name=form.cleaned_data["integration_name"],
            organization=form.cleaned_data["organization"],
        )

        obj.id = instance.id
        obj.long_lived_token_hash = instance.long_lived_token_hash
        obj.is_active = instance.is_active

        if instance.organization:
            access_message = f"with access to organization '{instance.organization.name}'"
        else:
            access_message = "with access to all organizations"

        self.message_user(
            request,
            f"System user '{instance.integration_name}' created successfully {access_message}. "
            f"Token: {instance.id}:{token}. Please store it safely. It will not be shown again.\n\n"
            f"eg. `Authorization: Bearer {instance.id}:{token}`",
            messages.SUCCESS,
        )
