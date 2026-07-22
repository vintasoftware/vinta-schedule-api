from typing import ClassVar

from django.db import models

from common.fields import OrganizationMembershipForeignKey
from common.models import BaseModel
from organizations.models import OrganizationModel
from public_api.constants import PublicAPIResources
from public_api.managers import SystemUserManager


class SystemUser(OrganizationModel):
    """
    Represents a system user in the application.
    This model is used to manage system-level users that interact with the application.
    """

    objects: SystemUserManager = SystemUserManager()

    organization = models.ForeignKey(
        "organizations.Organization",
        related_name="system_users",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    # Membership reference via the (organization_id, scoped_to_membership_user_id)
    # composite join rather than a real FK. Django 6 forbids a real FK to a
    # composite-PK model, and OrganizationMembership has a composite PK.
    # This contributes a concrete ``scoped_to_membership_user_id`` column plus a
    # ForeignObject descriptor ``scoped_to_membership``. NULL = organization-wide token.
    scoped_to_membership = OrganizationMembershipForeignKey(
        related_name="scoped_system_users",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text=(
            "When set, this token may only read/write data belonging to calendars owned by "
            "this organization membership's user. NULL = organization-wide token (legacy default)."
        ),
    )
    integration_name = models.CharField(max_length=150, unique=True, db_index=True)
    long_lived_token_hash = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Hash of the the system user's access token.",
    )
    is_active = models.BooleanField(default=True, help_text="Indicates if the user is active.")
    deleted_at = models.DateTimeField(null=True, blank=True, default=None, db_index=True)


class ResourceAccess(BaseModel):
    """
    Represents access permissions for a system user to specific resources.
    This model is used to manage which resources a system user can access.
    """

    system_user = models.ForeignKey(
        SystemUser,
        related_name="available_resources",
        on_delete=models.CASCADE,
    )
    resource_name = models.CharField(max_length=150, choices=PublicAPIResources, db_index=True)

    class Meta:
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["system_user", "resource_name"],
                name="uniq_resourceaccess_systemuser_resource",
            ),
        ]
