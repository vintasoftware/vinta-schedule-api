from typing import TYPE_CHECKING

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.utils.translation import gettext_lazy as _

from common.models import BaseModel
from s3direct_overrides.model_fields import S3DirectImageField

from .managers import UserManager


if TYPE_CHECKING:
    from payments.models import BillingProfile


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
    billing_profile: "BillingProfile"

    USERNAME_FIELD = "email"

    def get_full_name(self):
        return str(self.profile)

    def get_short_name(self):
        return self.profile.first_name

    def is_organization_admin(self, organization) -> bool:
        """True iff this user has an admin-role membership in `organization`.

        Accepts either an `Organization` instance or an id. Avoids importing
        the organizations app to prevent a circular import.
        """
        membership = getattr(self, "organization_membership", None)
        if membership is None:
            return False
        organization_id = getattr(organization, "id", organization)
        return membership.organization_id == organization_id and getattr(
            membership, "is_admin", False
        )

    def __str__(self):
        return f"{self.profile} <{self.email}>"


class Profile(BaseModel):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="profile", primary_key=True
    )
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    profile_picture = S3DirectImageField(dest="profile_pictures", blank=True, null=True)

    def __str__(self):
        return f"{self.first_name} {self.last_name}"
