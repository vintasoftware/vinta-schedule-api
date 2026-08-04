from typing import Annotated, Any

from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpRequest

from dependency_injector.wiring import Provide, inject

from organizations.models import Organization, OrganizationBranding
from organizations.slug_validation import validate_organization_slug
from payments.services.subscription_service import SubscriptionService


class OrganizationAdminForm(forms.ModelForm):
    """Rejects a ``parent`` selection that would create a cycle in the
    organization tree.

    ``parent`` is freely editable in admin with no other acyclicity check, which
    is how a cycle — the kind ``resolve_billing_root``'s cycle check exists to
    catch — gets created in the first place. It stays editable rather than
    read-only after creation: reparenting an organization between resellers is a
    legitimate admin operation with no other UI path, and this validation, not
    immutability, is what protects the rule.
    """

    # Explicitly declared as CharField (rather than left to ModelForm
    # auto-build from Organization.slug's models.SlugField, and NOT as
    # forms.SlugField) so Django does NOT auto-attach the SlugField's
    # ASCII-only RegexValidator: it would run before clean_slug() below and
    # preempt the confusables/reserved-word rules, which are the sole source
    # of format/confusable/reserved validation on this form.
    slug = forms.CharField(required=False)

    class Meta:
        model = Organization
        fields = (
            "name",
            "slug",
            "parent",
            "should_sync_rooms",
            "external_event_update_policy",
            "can_invite_organizations",
        )

    def clean_slug(self) -> str | None:
        """Run the shared slug rules, then check uniqueness against the DB.

        A blank submission normalizes to ``None`` (the model's NULL-when-unset
        contract) before the uniqueness check — otherwise two organizations both
        left blank would collide on the stored empty string, which is not the
        "multiple NULLs coexist" behavior the field is nullable for.
        """
        value = self.cleaned_data.get("slug")
        if not value:
            return None

        try:
            validate_organization_slug(value)
        except DjangoValidationError as exc:
            raise forms.ValidationError(exc.messages) from exc

        queryset = Organization.objects.filter(slug=value)
        if self.instance.pk is not None:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise forms.ValidationError(f"An organization with the slug '{value}' already exists.")
        return value

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
    reseller GraphQL mutation — ``save_model`` places a
    newly created organization on the default billing plan the same way
    ``OrganizationService.create_organization`` does, so an org created here is
    never left plan-less.

    ``can_invite_organizations`` is also toggleable on an existing organization via
    the "Reseller Capability" fieldset below, and flipping it on turns that org
    into its own billing root (``is_billing_root``). ``save_model`` therefore calls
    ``create_subscription_for_organization`` on every save, not just creation — it
    is idempotent (``get_or_create``) and already a no-op for non-roots, so it is
    safe to call unconditionally.
    """

    form = OrganizationAdminForm
    list_display = (
        "id",
        "name",
        "slug",
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
    search_fields = ("name", "id", "slug")
    ordering = ("-created",)
    readonly_fields = ("created", "modified", "id")

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "id",
                    "name",
                    "slug",
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
        """Persist ``obj`` and ensure it is on the default billing plan if it is a
        billing root — every organization that is its own billing root always has
        exactly one active plan; there is no plan-less state.

        Called on both create and edit: editing an existing organization can flip
        ``can_invite_organizations`` on, which turns it into a billing root
        (``is_billing_root``) that needs a ``Subscription`` it didn't have before.
        ``create_subscription_for_organization`` is idempotent and already a no-op
        for reseller children that pool against their billing root's subscription,
        so calling it unconditionally is correct on both paths.
        """
        super().save_model(request, obj, form, change)
        if subscription_service is None:
            raise RuntimeError(
                f"OrganizationAdmin.save_model: subscription_service not injected "
                f"(DI not wired?) — organization {obj.pk} saved with no Subscription "
                f"guarantee."
            )
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
                    "logo",
                    "primary_color",
                    "secondary_color",
                    "support_email",
                    "redirect_url",
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )
