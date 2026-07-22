from typing import TYPE_CHECKING, Annotated, Any

from django import forms
from django.contrib import (
    admin,
    messages,
)
from django.db.models import QuerySet
from django.http import HttpRequest

from dependency_injector.wiring import Provide, inject

from payments.billing_constants import LimitedResource
from payments.exceptions import OverLimitError
from public_api.models import ResourceAccess, SystemUser
from public_api.services import PublicAPIAuthService


if TYPE_CHECKING:
    from payments.services.entitlement_service import EntitlementService


class SystemUserAdminForm(forms.ModelForm):
    """Rejects a create that would exceed the organization's
    ``public_api_system_users`` ceiling *before* Django's admin
    ``_changeform_view`` reaches ``response_add``.

    Without this, ``SystemUserAdmin.save_model`` catches ``OverLimitError`` and
    returns ``None`` (no save), and ``_changeform_view`` still calls
    ``response_add(request, new_object)`` with ``new_object.pk is None``.
    ``response_add`` unconditionally reverses the change URL with that ``None``
    pk (``admin.utils.quote`` passes it through unchanged, and Django's admin
    object-URL pattern happens to accept the stringified ``"None"`` without
    raising ``NoReverseMatch`` on this version) and, since the default POST
    reaches neither the popup nor the ``"_continue"``/``"_addanother"`` branches
    that would use that malformed URL, falls through to a 302 redirect to the
    changelist -- with the ERROR message this ``save_model`` already queues
    *and* a bogus "was added successfully" SUCCESS message from ``response_add``
    itself, which has no idea the save was skipped. Confirmed via a direct
    request/response probe: no exception, but two contradictory messages and no
    row. This validation instead surfaces the limit as a single, correct field
    error on a 200 re-render, matching every other guarded surface's contract.

    Only guards creation: an edit (``self.instance.pk is not None``) does not
    add a new unit of usage, so it never needs this check, matching
    ``SystemUserAdmin.save_model``'s own ``change`` early-return. Uses
    ``check_limit`` without ``lock=True`` -- this is a UX pre-check, not the
    authoritative one; ``PublicAPIAuthService.create_system_user`` still takes
    the row lock and re-checks inside its own transaction at save time, so a
    concurrent create racing this validation is still caught correctly, just
    without a friendly form error.
    """

    class Meta:
        model = SystemUser
        fields = ("organization", "integration_name")

    @inject
    def clean(
        self,
        entitlement_service: Annotated[
            "EntitlementService | None", Provide["entitlement_service"]
        ] = None,
    ) -> dict[str, Any] | None:
        cleaned_data = super().clean()
        if self.instance.pk is not None or cleaned_data is None or entitlement_service is None:
            return cleaned_data

        organization = cleaned_data.get("organization")
        if organization is None:
            # Org-less tokens are unmetered by design (see create_system_user).
            return cleaned_data

        result = entitlement_service.check_limit(
            organization, LimitedResource.PUBLIC_API_SYSTEM_USERS
        )
        if not result.allowed:
            raise forms.ValidationError(
                {"organization": OverLimitError.from_check_result(result).as_error_body()["detail"]}
            )
        return cleaned_data


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
    form = SystemUserAdminForm
    list_display = ("organization", "integration_name", "is_active", "created", "modified")
    search_fields = ("integration_name", "organization__name")
    list_filter = ("organization", "created", "modified")
    readonly_fields = (
        "long_lived_token_hash",
        "created",
        "modified",
        "is_active",
        "scoped_to_membership",
    )
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "organization",
                    "integration_name",
                    "scoped_to_membership",
                    "long_lived_token_hash",
                    "is_active",
                )
            },
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
    def save_model(
        self,
        request: HttpRequest,
        obj: SystemUser,
        form: forms.Form,
        change: bool,
        public_api_auth_service: Annotated[
            PublicAPIAuthService | None, Provide["public_api_auth_service"]
        ] = None,
    ) -> None:
        # Injected here (per call), not on __init__: django.contrib.admin's
        # autodiscovery instantiates every registered ModelAdmin -- including this
        # one, since @admin.register runs at import time -- before di_core's
        # AppConfig.ready() calls container.wire() (django.contrib.admin is
        # imported earlier in INSTALLED_APPS than the internal apps di_core wires).
        # An @inject on __init__ would permanently freeze the globally-registered
        # singleton's public_api_auth_service at None. save_model is only ever
        # called per-request, well after wiring has completed, matching
        # OrganizationAdmin.save_model's identical pattern.
        if not public_api_auth_service:
            raise ValueError("PublicAPIAuthService is not provided")

        if change:
            return super().save_model(request, obj, form, change)

        try:
            instance, token = public_api_auth_service.create_system_user(
                integration_name=form.cleaned_data["integration_name"],
                organization=form.cleaned_data["organization"],
            )
        except OverLimitError as exc:
            # The organization is at its `public_api_system_users` ceiling. Show it as
            # an admin error message; letting it escape renders a 500 traceback for what
            # is an ordinary, actionable business outcome.
            self.message_user(request, exc.as_error_body()["detail"], messages.ERROR)
            return None

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
