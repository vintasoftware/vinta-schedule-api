from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.conf import settings
from django.core.exceptions import FieldDoesNotExist, ObjectDoesNotExist
from django.db import models

from common.fields import TenantSafeForeignKey, TenantSafeOneToOneField
from common.models import BaseModel
from organizations.managers import BaseOrganizationModelManager


if TYPE_CHECKING:
    from users.models import User


def get_active_organization_membership(
    user: User | None,
) -> OrganizationMembership | None:
    """Return the user's active OrganizationMembership, or None.

    This is the canonical helper for all tenant-access gates. Call it wherever
    a view, permission, or serializer needs to resolve an active membership:

        membership = get_active_organization_membership(request.user)
        if not membership:
            return <empty queryset / clean denial>

    Phase 1 implementation: returns the user's single active membership via a
    manager query. Phase 2a will layer the ``_active_membership`` stash so
    header-driven resolution is picked up here automatically.

    A user with no active memberships returns None (gated). An inactive
    membership (is_active=False) is treated identically to no membership.
    """
    if user is None:
        return None
    # Stable ordering: once later phases allow multiple active memberships, an
    # accidental two-active-row state must resolve deterministically. Multi-active
    # resolution becomes header-driven in Phase 2a, so this ordering is only a
    # deterministic fallback.
    return user.organization_memberships.filter(is_active=True).order_by("created").first()  # type: ignore[union-attr]


class OrganizationTier(BaseModel):
    """
    Represents a tier for a calendar organization.
    """

    name = models.CharField(max_length=255)

    def __str__(self):
        return self.name


class Organization(BaseModel):
    """
    Represents a calendar organization.
    """

    name = models.CharField(max_length=255)
    tier = models.ForeignKey(
        OrganizationTier,
        on_delete=models.CASCADE,
        related_name="organizations",
        null=True,
    )
    should_sync_rooms = models.BooleanField(
        default=False, help_text="Whether to sync rooms for this organization."
    )

    def __str__(self):
        return self.name


class SubscriptionPlan(BaseModel):
    """
    Represents a subscription plan for a calendar organization.
    """

    value = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    billing_day = models.IntegerField()
    tier = models.ForeignKey(
        OrganizationTier,
        on_delete=models.CASCADE,
        related_name="subscription_plans",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="subscription_plans",
    )
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class OrganizationForeignKey(TenantSafeForeignKey):
    """
    A ForeignKey that enforces the tenant_id in JOIN ON clauses.
    This is used to ensure that calendar organizations are properly scoped to the tenant.
    """

    tenant_field = "organization_id"


class OrganizationOneToOneField(TenantSafeOneToOneField):
    """
    A OneToOneField that enforces the tenant_id in JOIN ON clauses.
    This is used to ensure that calendar organizations are properly scoped to the tenant.
    """

    tenant_field = "organization_id"


class OrganizationRole(models.TextChoices):
    """Role a user holds within an organization.

    A flat two-role model — enough for current permission needs. Richer
    hierarchies (e.g. owner/admin/member) can be layered later without a
    disruptive migration.
    """

    MEMBER = "member", "Member"
    ADMIN = "admin", "Admin"


class OrganizationMembership(BaseModel):
    """
    Represents a membership of a user in a calendar organization.
    This is used to link users to their respective calendar organizations.

    Hard-gate invariant:
        Every authenticated user is in exactly one of two states:
        1. **Has active membership** — ``get_active_organization_membership(user)``
           returns an ``OrganizationMembership`` instance and all tenant-scoped
           endpoints are open to them.
        2. **Gated (zero active memberships)** — ``get_active_organization_membership``
           returns ``None``. Only the onboarding surfaces respond:
           ``POST /organizations/`` (create own org) and ``POST /invitations/accept``
           (join an invited org). All other tenant-scoped endpoints must return an
           empty queryset or permission denial — never a 500.

        A user may hold memberships in multiple organizations. Resolution of the
        *active* organization is handled by ``get_active_organization_membership``,
        which in Phase 1 returns the user's single active membership. Phase 2a
        introduces header-driven resolution for multi-org users.

        Never read ``user.organization_memberships`` directly in permission /
        scoping code — always go through ``get_active_organization_membership`` so
        the resolution seam is centralised.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organization_memberships",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    role = models.CharField(
        max_length=20,
        choices=OrganizationRole,
        default=OrganizationRole.MEMBER,
        help_text=(
            "Role the user holds in this organization. Admins can manage "
            "organization-scoped resources (e.g. CalendarGroups) regardless of "
            "direct ownership."
        ),
    )
    is_active = models.BooleanField(
        default=True,
        db_default=True,
        db_index=True,
        help_text=(
            "Whether this membership is active. Inactive memberships are treated as "
            "gated: the user still has a row but loses all tenant-scoped access until "
            "reactivated. Use this to disable a user without deleting their membership "
            "record (which would lose role/history). Default True keeps every existing "
            "read unchanged."
        ),
    )

    class Meta:
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["user", "organization"],
                name="uniq_membership_user_organization",
            ),
        ]

    def __str__(self):
        return f"{self.user} in {self.organization}"

    @property
    def is_admin(self) -> bool:
        """True if this membership confers admin rights in the organization."""
        return self.role == OrganizationRole.ADMIN


class OrganizationInvitation(BaseModel):
    """
    Represents an invitation to join a calendar organization.
    This is used to invite users to join their respective calendar organizations.
    """

    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="invitations",
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sent_organization_invitations",
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    token_hash = models.TextField()
    expires_at = models.DateTimeField()
    membership = models.OneToOneField(
        OrganizationMembership,
        on_delete=models.CASCADE,
        related_name="invitation",
        null=True,
        blank=True,
    )

    def __str__(self):
        return f"Invitation for {self.email} to join {self.organization}"


class OrganizationModel(BaseModel):
    """
    Represents a model that can be associated with a calendar organization.
    This is used to link calendars to an organization.
    """

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="+",
        help_text="The organization this model is associated with. Queries should use the `organization` field.",
    )

    objects: BaseOrganizationModelManager = BaseOrganizationModelManager()
    original_manager = models.Manager()

    class Meta:
        abstract = True

    @classmethod
    def is_field_organization_foreign_key(cls, field: models.Field) -> bool:
        try:
            fk_field = cls._meta.get_field(f"{field.name}_fk")
        except FieldDoesNotExist:
            fk_field = None

        return (
            isinstance(field, models.ForeignObject)
            and bool(fk_field)
            and isinstance(fk_field, models.ForeignKey)
        )

    def __init__(self, *args, **kwargs):
        # find model fields that are OrganizationForeignKey
        foreign_key_fields_in_kwargs = [
            field.name
            for field in self._meta.get_fields()
            if (
                self.is_field_organization_foreign_key(field)
                and (field.name in kwargs.keys() or f"{field.name}_id" in kwargs.keys())
            )
        ]

        for field_name in foreign_key_fields_in_kwargs:
            if field_name in kwargs.keys() and not kwargs.get(f"{field_name}_fk", None):
                kwargs[f"{field_name}_fk"] = kwargs.pop(field_name)
                continue
            if f"{field_name}_id" in kwargs.keys() and not kwargs.get(f"{field_name}_fk_id", None):
                kwargs[f"{field_name}_fk_id"] = kwargs.pop(f"{field_name}_id")
                continue

        super().__init__(*args, **kwargs)

    def save(self, *args, **kwargs):
        # find model fields that are OrganizationForeignKey
        foreign_key_fields = [
            field.name
            for field in self._meta.get_fields()
            if (self.is_field_organization_foreign_key(field))
        ]

        is_create = self.id is None

        if is_create:
            for field_name in foreign_key_fields:
                try:
                    foreign_object_field_value = getattr(self, field_name, None)
                except (FieldDoesNotExist, ObjectDoesNotExist):
                    foreign_object_field_value = None
                if foreign_object_field_value and not getattr(self, f"{field_name}_fk", None):
                    setattr(self, f"{field_name}_fk", foreign_object_field_value)
        else:
            for field_name in foreign_key_fields:
                old_instance = self.__class__.original_manager.filter(id=self.id).first()
                try:
                    foreign_object_field_value = getattr(self, field_name, None)
                except (FieldDoesNotExist, ObjectDoesNotExist):
                    foreign_object_field_value = None

                if old_instance and foreign_object_field_value != getattr(
                    old_instance, field_name, None
                ):
                    self.organization = old_instance.organization
                    setattr(self, f"{field_name}_fk", foreign_object_field_value)

        return super().save(*args, **kwargs)
