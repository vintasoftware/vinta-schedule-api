from typing import TYPE_CHECKING

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.core.validators import MinLengthValidator, RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from common.models import BaseModel
from s3direct_overrides.model_fields import S3DirectImageField

from .managers import UserManager


if TYPE_CHECKING:
    from payments.models import BillingProfile


class User(AbstractBaseUser, PermissionsMixin, BaseModel):
    username = models.CharField(
        max_length=150,
        unique=True,
        help_text=_("Required. 150 characters or fewer. Letters, digits and @/./+/-/_ only."),
        validators=[
            RegexValidator(
                regex=r"^[\w.-]+$",
                message=_(
                    "Enter a valid username. This value may contain only letters, numbers and ./-/_ characters."
                ),
            ),
            MinLengthValidator(
                limit_value=3,
                message=_("Username must be at least 3 characters long."),
            ),
        ],
        db_index=True,
    )
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

    USERNAME_FIELD = "username"

    def get_full_name(self):
        return str(self.profile)

    def get_short_name(self):
        return self.profile.first_name

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
