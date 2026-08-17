from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import Group
from django.utils.translation import gettext_lazy as _

from organizations.permission_catalog import GROUP_PERMISSIONS

from .models import Profile, User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ("id", "email", "created", "modified")
    list_filter = ("is_active", "is_staff", "groups")
    search_fields = ("email",)
    ordering = ("email",)
    filter_horizontal = (
        "groups",
        "user_permissions",
    )

    def get_form(self, request, obj=None, **kwargs):
        """Stash ``obj`` on ``request`` so ``formfield_for_manytomany`` can see it.

        ``ModelAdmin.get_form`` never forwards the object being edited down to
        ``formfield_for_manytomany`` -- the ``formfield_callback`` it builds
        only carries ``request``. Attaching it to the request (rather than
        ``self``, which is a single ``ModelAdmin`` instance shared by every
        concurrent request) keeps this request-scoped.
        """
        request._organization_seeded_groups_obj = obj
        return super().get_form(request, obj, **kwargs)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        """Keep the organization groups out of the *user*-level group picker.

        ``organization_admin`` / ``organization_billing_owner`` /
        ``organization_member`` are seeded ``auth.Group`` rows, but they are
        only meaningful hanging off an **OrganizationMembership** -- that is
        what makes a grant organization-scoped. Attached to a *user* they grant
        nothing: since Phase 4 of the vinta-django-orgs migration every
        authorization check resolves the organization half alone
        (``organizations.authorization.has_organization_permission``), which
        never looks at ``user.groups``.

        They were listed here, in a picker labelled "Groups", beside the real
        permission controls -- so the obvious way for a staff user to "make
        somebody an admin" was to tick a box that does nothing. The assignment
        was inert before and after Phase 4 because organization capabilities are
        resolved from membership groups, not global user groups; what is left is
        a control whose only possible outcome is a wrong belief. Membership groups are
        assigned through ``POST /organization-members/{user_id}/groups/``.

        Only the *widget's* choices are narrowed. Any other ``auth.Group`` a
        deployment defines stays selectable, and an existing (pre-Phase-4)
        assignment of a seeded group is left on the row rather than silently
        dropped -- it is inert either way, and removing data from a form the
        operator did not touch is worse than leaving it.

        That "left on the row" promise has teeth here: ``ModelMultipleChoiceField``
        only renders (and, on save, only keeps -- ``ModelForm.save_m2m`` calls
        ``.set()`` on the submitted set) options present in the field's
        ``queryset``. Excluding the seeded groups outright would silently strip
        an existing assignment the moment the form is saved for any other
        reason. So the exclusion applies to *new* choices only; a seeded group
        the object being edited already holds is unioned back in. The add form
        has no ``obj``, so it gets the plain exclusion.
        """
        if db_field.name == "groups":
            excluded_names = tuple(GROUP_PERMISSIONS)
            queryset = Group.objects.exclude(name__in=excluded_names)
            obj = getattr(request, "_organization_seeded_groups_obj", None)
            if obj is not None:
                queryset = queryset | Group.objects.filter(name__in=excluded_names, user=obj)
            kwargs["queryset"] = queryset
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (
            _("Permissions"),
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("email", "password1", "password2")}),)


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("first_name", "last_name", "profile_picture")
    search_fields = ("first_name", "last_name")
    list_filter = ("user__is_active", "user__is_staff")
    fieldsets = ((_("Personal Info"), {"fields": ("first_name", "last_name", "profile_picture")}),)
