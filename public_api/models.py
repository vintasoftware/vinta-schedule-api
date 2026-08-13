from typing import Any, ClassVar

from django.db import models

from vinta_orgs.fields import expand_safe_relation_field_names
from vinta_orgs.mixins import SingleOrganizationModelMixin

from common.fields import OrganizationMembershipForeignKey
from common.models import BaseModel, SafeRelationNullInitMixin, UnscopedUniqueChecksMixin
from public_api.constants import PublicAPIResources
from public_api.managers import SystemUserManager


class SystemUser(
    SingleOrganizationModelMixin,
    SafeRelationNullInitMixin,
    UnscopedUniqueChecksMixin,
    BaseModel,
):
    """
    Represents a system user in the application.
    This model is used to manage system-level users that interact with the application.

    **The one scoped model in this project whose ``organization`` is nullable.**
    ``organization=None`` is the org-less credential: a token with access to every
    organization, mintable only from the Django admin, documented in
    ``PublicAPIAuthService.create_system_user``. Two consequences of keeping that
    state while adopting ``SingleOrganizationModelMixin``:

    * ``objects`` is implicitly scoped, so an org-less row is invisible to it --
      exactly as it was to the ``organization``-filter-requiring manager this
      replaces. ``original_manager`` is how those rows are read (see
      ``PublicAPIAuthService.check_system_user_token``, which resolves a token by
      its globally-unique id before any organization is known).
    * ``save()`` is overridden below so the mixin cannot stamp one with whatever
      organization happens to be bound.
    """

    objects: ClassVar[SystemUserManager] = SystemUserManager()

    # ``type: ignore[assignment]``: the base declares ``organization`` non-null;
    # this model's is deliberately nullable -- see the class docstring.
    organization = models.ForeignKey(  # type: ignore[assignment]
        "organizations.Organization",
        related_name="system_users",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        # Matches ``SingleOrganizationModelMixin.organization``: the single-column
        # index Django would build here is a prefix of the ``(organization, id)``
        # index the package's ``class_prepared`` receiver adds, so it can never
        # answer a query the composite cannot.
        db_index=False,
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

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Persist an org-less token as org-less instead of adopting the context.

        ``SingleOrganizationModelMixin.save()`` resolves
        ``get_current_organization() or get_default_organization()`` whenever
        ``organization_id`` is ``None`` and raises ``OrganizationNotFoundError``
        when neither answers. Both halves are wrong for this model: it is the one
        scoped model where ``organization=None`` is a *value* rather than a
        forgotten argument, so raising would break the supported admin path and
        resolving would silently narrow a deliberately global credential to
        whichever organization the caller happened to be serving.

        Everything else the mixin's ``save()`` does is kept -- the
        ``update_fields`` expansion below is the whole of the rest -- and a row
        that *does* name an organization takes the mixin's path unchanged.
        """
        if self.organization_id is not None:
            return super().save(*args, **kwargs)

        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = expand_safe_relation_field_names(
                self.__class__, kwargs["update_fields"]
            )
        # Skips exactly one class in the MRO -- the mixin -- and nothing below it.
        return super(SingleOrganizationModelMixin, self).save(*args, **kwargs)


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
