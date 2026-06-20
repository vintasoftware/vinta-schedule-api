from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import FieldDoesNotExist, ObjectDoesNotExist
from django.db import models

from common.fields import (
    OrganizationMembershipForeignKey,
    TenantSafeForeignKey,
    TenantSafeOneToOneField,
)
from common.models import BaseModel
from organizations.managers import BaseOrganizationModelManager, OrganizationMembershipManager


if TYPE_CHECKING:
    from users.models import User


# Sentinel distinguishes "not resolved yet" (off-DRF path) from ``None``
# (resolved-to-gated / resolved-to-no-membership). Must be module-level so
# the same object identity is checked everywhere.
_UNSET: object = object()


def get_active_organization_membership(
    user: User | None,
) -> OrganizationMembership | None:
    """Return the user's active OrganizationMembership, or None.

    This is the canonical helper for all tenant-access gates. Call it wherever
    a view, permission, or serializer needs to resolve an active membership:

        membership = get_active_organization_membership(request.user)
        if not membership:
            return <empty queryset / clean denial>

    On the DRF request path (Phase 2a+), ``TenantScopedViewMixin.initial()``
    resolves the active membership from the ``X-Organization-Id`` header and
    stashes it on ``user._active_membership``. This helper reads the stash so
    the ~60 existing call sites are automatically header-aware without change.

    Off the DRF request path (management commands, Celery tasks, tests that
    bypass views), ``_active_membership`` is absent and the helper falls back
    to the single-membership query so those callers keep working.

    A user with no active memberships returns None (gated). An inactive
    membership (is_active=False) is treated identically to no membership.
    """
    if user is None:
        return None

    stashed = getattr(user, "_active_membership", _UNSET)
    if stashed is not _UNSET:
        # DRF request path: the resolver has already run; trust its result
        # (may be an OrganizationMembership or None for gated users).
        return stashed  # type: ignore[return-value]

    # Off-request path (management commands, Celery tasks, direct test calls):
    # fall back to the single active membership query. Stable ordering ensures
    # determinism if a user somehow ends up with two active memberships here.
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
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="child_organizations",
        help_text=(
            "The parent organization if this is a child org. "
            "A reseller with live children cannot be deleted."
        ),
    )
    can_invite_organizations = models.BooleanField(
        default=False,
        help_text=(
            "Whether this organization can invite/create other organizations. "
            "DB/Django-admin only — never exposed via any API. "
            "Enables the whole reseller capability bundle."
        ),
    )

    class Meta:
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["parent", "name"],
                name="uniq_org_name_per_parent",
            ),
        ]

    def __str__(self):
        return self.name

    def is_reseller(self) -> bool:
        """Return True if this org can invite/create other organizations."""
        return self.can_invite_organizations

    def get_branding_root(self) -> Organization | None:
        """
        Walk up the parent chain to the nearest ancestor with can_invite_organizations=True.

        Returns the reseller ancestor (which has branding), or None if no such ancestor exists.
        The None case means this org (or its entire lineage) has no reseller, so vinta defaults apply.
        """
        seen: set[int] = set()
        org: Organization | None = self
        while org is not None and org.pk not in seen:
            if org.can_invite_organizations:
                return org
            seen.add(org.pk)
            org = org.parent
        return None


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

    objects: OrganizationMembershipManager = OrganizationMembershipManager()

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

    The uniqueness constraint is ``unique(email, organization)`` — the same email address
    may hold concurrent pending invitations in different organizations (multi-org invite
    accept, Phase 4). A duplicate invite to the *same* org is still rejected by the
    ``uniq_invitation_email_organization`` constraint.
    """

    email = models.EmailField()
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="invitations",
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="sent_organization_invitations",
        null=True,
        blank=True,
    )
    role = models.CharField(
        max_length=20,
        choices=OrganizationRole,
        default=OrganizationRole.MEMBER,
        help_text=(
            "Role the invited user should receive on accepting the invitation. "
            "Defaults to MEMBER. Admin invitations must be explicit."
        ),
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    token_hash = models.TextField()
    expires_at = models.DateTimeField()
    # Membership reference via the (organization_id, membership_user_id) composite join
    # rather than a real FK. Django 6 forbids a real FK to a composite-PK model
    # (OrganizationMembership becomes composite-PK in Phase 7b). This contributes a
    # concrete ``membership_user_id`` column plus a ForeignObject descriptor ``membership``.
    # OneToOne semantics are preserved by the partial UniqueConstraint below
    # (one accepted invitation per membership). ``related_name="invitation"`` keeps the
    # ``membership.invitation`` reverse accessor (now a reverse manager).
    membership = OrganizationMembershipForeignKey(
        on_delete=models.CASCADE,
        related_name="invitation",
        null=True,
        blank=True,
    )

    class Meta:
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["email", "organization"],
                name="uniq_invitation_email_organization",
            ),
            models.UniqueConstraint(
                fields=["organization", "membership_user_id"],
                condition=models.Q(membership_user_id__isnull=False),
                name="uniq_invitation_membership_user_per_org",
            ),
        ]

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


class OrganizationBranding(models.Model):
    """
    Stores branding customization for a reseller organization.

    A one-to-one relationship with an Organization (expected to be a reseller).
    Child organizations resolve their branding by walking up the parent chain
    to the nearest reseller ancestor and using its branding row. If no reseller
    ancestor has a branding row, the vinta default is used.
    """

    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="branding",
        help_text="The reseller organization this branding customizes.",
    )
    app_name = models.CharField(
        max_length=120,
        help_text="The display name of the white-labeled app (e.g., 'MyScheduler').",
    )
    logo_url = models.URLField(
        blank=True,
        default="",
        help_text="URL to the reseller's logo image.",
    )
    primary_color = models.CharField(
        max_length=9,
        blank=True,
        default="",
        help_text="Primary color as hex code: #RRGGBB or #RRGGBBAA.",
    )
    secondary_color = models.CharField(
        max_length=9,
        blank=True,
        default="",
        help_text="Secondary color as hex code: #RRGGBB or #RRGGBBAA.",
    )
    support_email = models.EmailField(
        blank=True,
        default="",
        help_text="Email address for the From/reply-to on branded transactional emails.",
    )
    return_url_allowlist = ArrayField(
        models.URLField(),
        default=list,
        blank=True,
        help_text="List of URLs that are allowed as return addresses after OAuth flows.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Organization Branding"
        verbose_name_plural = "Organization Brandings"

    def __str__(self):
        return f"Branding for {self.organization.name}"


def resolve_branding(org: Organization) -> OrganizationBranding | None:
    """
    Resolve branding for an organization, walking up the parent chain to the reseller.

    If the organization itself is a reseller, returns its branding row (or None if unset).
    Otherwise, walks up the parent chain to find the nearest reseller ancestor and
    returns its branding row (or None if the reseller has no branding row).

    If no reseller ancestor exists, returns None (vinta default branding applies).

    Args:
        org: The Organization instance to resolve branding for.

    Returns:
        The OrganizationBranding row of the reseller ancestor, or None if unset/no reseller.
    """
    branding_root = org.get_branding_root()
    if branding_root is None:
        return None
    return getattr(branding_root, "branding", None)
