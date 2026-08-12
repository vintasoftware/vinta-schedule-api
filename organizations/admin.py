"""Admin for ``Organization`` and ``OrganizationBranding`` -- intentionally cross-organization.

Phase 0 of the vinta-django-orgs migration
(``ai-plans/2026-08-12-VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md``) asks every
admin here to route its org-scoped querysets through ``original_manager`` rather than
binding an ``organization_context``, so the cross-org intent is written down rather than
inferred (see ``audit/admin.py::AuditAdmin.get_queryset`` for the precedent this mirrors).
There is nothing to change in *this* file to satisfy that: neither ``Organization`` nor
``OrganizationBranding`` is an ``OrganizationModel`` subclass -- ``Organization`` is the
tenant itself, and ``OrganizationBranding`` is a plain ``models.Model`` keyed on it -- so
every ``.objects`` query below (``Organization.objects.filter(slug=...)`` in
``OrganizationAdminForm.clean_slug``, and the admin's own unfiltered changelist queries)
already runs on Django's stock, unscoped manager, not
``organizations.managers.BaseOrganizationModelManager``. This note exists so that fact is
recorded rather than silently assumed.
"""

from typing import Annotated, Any

from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpRequest

# Imported for its registration side effect, so the unregistration below runs
# after it no matter which order ``admin.autodiscover()`` reaches the two apps
# in. Without this, ours could be imported first and the package's
# ``admin.site.register`` would then raise ``AlreadyRegistered``.
import vinta_orgs.admin  # noqa: F401
from dependency_injector.wiring import Provide, inject

from organizations.models import Organization, OrganizationBranding, OrganizationMembership
from organizations.slug_generation import opaque_organization_slug
from organizations.slug_validation import validate_organization_slug
from payments.services.subscription_service import SubscriptionService


# ``vinta_orgs.admin`` registers a ``ModelAdmin`` against whatever
# ``ORGANIZATION_MODEL`` / ``ORGANIZATION_MEMBERSHIP_MODEL`` name -- which, since
# this project swapped both, means *our* models. Both registrations are dropped
# here, using the supported ``unregister`` call the package's own admin module
# points projects at.
#
# This is an authorization change, not tidying:
#
# * ``OrganizationMembershipAdmin`` exposes ``role``, ``is_billing_owner`` and
#   ``groups`` as plain, staff-editable fields, with none of the rules the REST
#   viewset enforces -- the seat limit, and the refusal to demote the last
#   active admin in an organization. Any staff user with the change permission
#   could grant themselves organization admin or billing ownership in a single
#   form post. It is left unregistered outright; a membership admin that
#   carries those rules is Phase 3's, not this phase's.
# * ``OrganizationAdmin`` is replaced (below) rather than merely dropped: ours
#   validates the slug, refuses a parent cycle, and puts every new organization
#   on a billing plan. The package's also inlines ``OrganizationSite``, which we
#   do not use at all (domain-based tenancy is a Non-goal).
for _package_registered_model in (Organization, OrganizationMembership):
    if admin.site.is_registered(_package_registered_model):
        admin.site.unregister(_package_registered_model)
del _package_registered_model


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

    # Explicitly declared, and ``required=False`` where the model field is not,
    # so a blank submission reaches ``clean_slug`` below (which mints an opaque
    # slug on create and refuses the clear on update) instead of being rejected
    # by the auto-built field with a bare "This field is required."
    slug = forms.CharField(required=False)

    class Meta:
        model = Organization
        fields = (
            "name",
            "slug",
            "parent",
            "should_sync_rooms",
            "external_event_update_policy",
            "week_start",
            "can_invite_organizations",
        )

    def clean_slug(self) -> str:
        """Run the shared slug rules, then check uniqueness against the DB.

        ``slug`` is NOT NULL and the ``organization_slug_not_blank`` check
        constraint refuses ``""``, so a blank submission has to resolve to
        *something*:

        * On an existing organization it is refused. Clearing the slug is not a
          supported operation -- and because ``Organization.save()`` mints a
          replacement for an empty slug, silently accepting it would swap the
          organization's public identifier for a different one rather than
          removing it, orphaning every branded login URL already issued.
        * On a new organization it mints an **opaque** ``org-<token>`` slug.
          Deliberately not ``slugify(name)``: the slug is public, and an
          operator creating an organization on someone's behalf has not
          consented to publishing its name. The organization can pick a
          readable slug itself later.
        """
        value = self.cleaned_data.get("slug")
        if not value:
            if self.instance.pk is not None:
                raise forms.ValidationError(
                    "Slug cannot be cleared once set. Enter a new slug, or leave this "
                    "field as it was."
                )
            return opaque_organization_slug(
                slug_exists=lambda candidate: Organization.objects.filter(slug=candidate).exists()
            )

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
        "week_start",
        "parent",
        "created",
        "modified",
    )
    list_filter = (
        "can_invite_organizations",
        "external_event_update_policy",
        "week_start",
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
                    "week_start",
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


class OrganizationBrandingAdminForm(forms.ModelForm):
    """Refuses to save branding for an organization that has a parent.

    Admin is not an escape hatch: "no branding for organizations inside a
    hierarchy" (see the plan's Non-goals) holds for staff too, mirroring here
    what the other two write surfaces (``OrganizationBrandingView``,
    ``update_branding``) enforce via ``organizations.permissions.
    evaluate_branding_write_gate``. Deliberately checks ONLY the parent
    condition, not entitlement or slug: those are self-serve/billing states an
    operator may legitimately need to seed branding ahead of (e.g. before the
    organization's own admin has picked a slug), whereas the parent rule is a
    hard structural boundary with no legitimate admin override.
    """

    class Meta:
        model = OrganizationBranding
        fields = (
            "organization",
            "app_name",
            "logo",
            "primary_color",
            "secondary_color",
            "support_email",
            "redirect_url",
        )

    def clean(self) -> dict[str, Any] | None:
        cleaned_data = super().clean()
        organization = None if cleaned_data is None else cleaned_data.get("organization")
        if organization is not None and organization.parent_id is not None:
            raise forms.ValidationError(
                {
                    "organization": (
                        "This organization has a parent organization and cannot have "
                        "its own branding. Branding for organizations inside a "
                        "hierarchy is controlled by the reseller organization above them."
                    )
                }
            )
        return cleaned_data


@admin.register(OrganizationBranding)
class OrganizationBrandingAdmin(admin.ModelAdmin):
    """Admin interface for OrganizationBranding.

    ``form`` refuses to save branding for an organization that has a parent
    (``OrganizationBrandingAdminForm.clean``) -- the parent-present rule holds
    for staff too, not just the REST/GraphQL write surfaces.
    """

    form = OrganizationBrandingAdminForm
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
