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
``tenancy.managers.BaseOrganizationModelManager``. This note exists so that fact is
recorded rather than silently assumed.

Admin double registration (Phase 1c)
------------------------------------
``vinta-django-orgs``' own ``organizations/admin.py`` ends with::

    admin.site.register(get_organization_model(), OrganizationAdmin)
    admin.site.register(get_organization_membership_model(), OrganizationMembershipAdmin)

Those two calls resolve the *swappable* settings, so on this project they
register ``tenancy.Organization`` and ``tenancy.OrganizationMembership`` -- and
``admin.site.register`` refuses a model it already knows, so whichever of the two
``admin`` modules ``autodiscover()`` imports second raises ``AlreadyRegistered``.
The package's own docstring names the supported resolution: "A project that
swapped either one and wants a different admin for it should unregister first."

That is what the module-level block below does. Importing ``organizations.admin``
explicitly makes the ordering deterministic rather than a function of
``INSTALLED_APPS`` order (``autodiscover_modules`` is a no-op for an already
imported module, so whichever order it walks, the package's registrations exist
by the time ours are installed and are dropped exactly once). It is also
re-entrant in the two ways that matter: the module body runs once per process,
so a second ``AdminConfig.ready()`` -- which ``override_settings(INSTALLED_APPS=...)``
triggers by repopulating the app registry -- re-imports nothing and leaves
``admin.site._registry`` holding *our* ``ModelAdmin``; and the unregister is
guarded on ``is_registered`` so it cannot raise ``NotRegistered`` if the package
ever stops registering.

``OrganizationMembership`` has no ``ModelAdmin`` of ours to collide with, but
the package's own ``OrganizationMembershipAdmin`` is not left registered
either: it gives staff full add/change/delete over ``OrganizationMembership``
with ``role``, ``is_billing_owner``, ``is_active``, and the ``groups`` /
``permissions`` M2Ms all editable, none of the "protect the last active
admin" or seat-limit rules the REST viewset (``tenancy/views.py``) enforces,
and with ``groups`` / ``permissions`` semantics undefined until Phase 3. The
brief this admin resolves is the registration *collision*, not "adopt
whatever membership admin the package ships" -- so it is unregistered here
too, leaving the project with no Django-admin surface for memberships at all
until a real one (respecting the same invariants the REST viewset does) is
deliberately built in Phase 3.

No ``sys.modules`` patching: the Phase 1a attempt at that was rejected in review,
and nothing here needs it.
"""

from typing import Annotated, Any

from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpRequest

import organizations.admin  # noqa: F401 -- imported for its registration side effect
from dependency_injector.wiring import Provide, inject

from payments.services.subscription_service import SubscriptionService
from tenancy.models import Organization, OrganizationBranding, OrganizationMembership
from tenancy.slug_validation import validate_organization_slug


if admin.site.is_registered(Organization):
    # The package registered it (see the module docstring). Ours replaces it.
    admin.site.unregister(Organization)

if admin.site.is_registered(OrganizationMembership):
    # The package's OrganizationMembershipAdmin -- see the module docstring
    # for why this project does not adopt it as-is.
    admin.site.unregister(OrganizationMembership)


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
            "week_start",
            "can_invite_organizations",
        )

    def clean_slug(self) -> str | None:
        """Run the shared slug rules, then check uniqueness against the DB.

        A blank submission is refused on an organization that already has a slug:
        the column became NOT NULL in Phase 1c of the vinta-django-orgs migration
        and "unset it" is no longer expressible. Mirrors
        ``OrganizationSerializer.validate_slug`` deliberately — the two write
        surfaces must not disagree about what blank means. On the *add* form,
        blank still passes through as ``None`` and ``Organization.save()`` derives
        one from the name.
        """
        value = self.cleaned_data.get("slug")
        if not value:
            if self.instance.pk is not None and self.instance.slug:
                raise forms.ValidationError(
                    "An organization's slug cannot be cleared. Submit a new slug instead."
                )
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
    ``update_branding``) enforce via ``tenancy.permissions.
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
