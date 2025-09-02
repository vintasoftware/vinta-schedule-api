from django.db import models

from common.models import BaseModel
from public_api.constants import PublicAPIResources


class SystemUser(BaseModel):
    """
    Represents a system user in the application.
    This model is used to manage system-level users that interact with the application.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        related_name="system_users",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    integration_name = models.CharField(max_length=150, unique=True, db_index=True)
    long_lived_token_hash = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Hash of the the system user's access token.",
    )
    is_active = models.BooleanField(default=True, help_text="Indicates if the user is active.")


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
