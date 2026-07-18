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
        """True iff this user has an active admin-role membership in `organization`.

        Accepts either an `Organization` instance or an id. Avoids importing
        the organizations app to prevent a circular import.

        An inactive membership (is_active=False) is treated the same as no
        membership — returns False to deny access.
        """
        from organizations.models import OrganizationRole

        organization_id = getattr(organization, "id", organization)
        return self.organization_memberships.filter(  # type: ignore[attr-defined]
            organization_id=organization_id,
            is_active=True,
            role=OrganizationRole.ADMIN,
        ).exists()

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
