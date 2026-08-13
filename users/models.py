from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.utils.translation import gettext_lazy as _

from common.models import BaseModel
from s3direct_overrides.model_fields import S3DirectImageField

from .managers import UserManager


class User(AbstractBaseUser, PermissionsMixin, BaseModel):
    email = models.EmailField(max_length=255, unique=True)
    phone_number = models.CharField(max_length=20)

    phone_verified_date = models.DateTimeField(null=True, blank=True)
    is_staff = models.BooleanField(
        default=False,
        help_text=_("Designates whether the user can log into this admin site."),
    )
    is_active = models.BooleanField(
        default=True,
        help_text=_(
            "Designates whether this user should be treated as "
            "active. Unselect this instead of deleting accounts."
        ),
    )

    objects: UserManager = UserManager()
    profile: "Profile"

    USERNAME_FIELD = "email"

    def get_full_name(self):
        return str(self.profile)

    def get_short_name(self):
        return self.profile.first_name

    def is_organization_admin(self, organization) -> bool:
        """True iff this user may administer `organization`.

        Accepts either an `Organization` instance or an id, and keeps that
        signature deliberately: this is the shared "is the caller an admin
        here" question, called from `calendar_integration`'s permission
        classes, its views, and `CalendarPermissionService`, and every one of
        them names the organization explicitly rather than relying on whatever
        is bound.

        Reads `organizations.manage_members` rather than `role == ADMIN` (Phase
        4 of the vinta-django-orgs migration). The outcome is unchanged for
        every membership the system writes: the `organization_admin` group
        carries that permission and nothing else grants it.

        Still membership-bounded, which is what the "iff" above is worth: the
        permission is resolved from an active membership in `organization`
        alone, so neither a global `user_permissions` grant, nor membership of
        the global `organization_admin` group, nor `is_superuser` answers
        `True` here for an organization this user does not belong to. See
        `organizations.authorization.has_organization_permission`, which owns
        that rule, and `organizations.auth_backends.OrganizationModelBackend`,
        which enforces the membership's own `is_active` gate one layer further
        down.

        Imported inside the method to avoid a circular import at module load.
        """
        from organizations.authorization import has_organization_permission
        from organizations.permission_catalog import MANAGE_MEMBERS

        return has_organization_permission(self, MANAGE_MEMBERS, organization)

    def __str__(self):
        return f"{self.profile} <{self.email}>"


class Profile(BaseModel):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="profile", primary_key=True
    )
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    profile_picture = S3DirectImageField(dest="profile_pictures", blank=True, null=True)
    pending_organization_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=(
            "Intended organization name captured at email/password signup. "
            "Consumed and cleared when the org is created on email confirmation. "
            "Blank for invited signups (they auto-join, no org name needed)."
        ),
    )

    def __str__(self):
        return f"{self.first_name} {self.last_name}"
