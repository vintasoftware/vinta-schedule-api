from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar

from django.conf import settings
from django.core.exceptions import FieldDoesNotExist, ObjectDoesNotExist
from django.db import models

from organizations.models import AbstractOrganization, AbstractOrganizationMembership

from common.fields import (
    OrganizationMembershipForeignKey,
    TenantSafeForeignKey,
    TenantSafeOneToOneField,
)
from common.models import BaseModel
from payments.billing_constants import Entitlement
from payments.entitlement_cache import has_entitlement_cached
from s3direct_overrides.model_fields import S3DirectImageField
from tenancy.managers import (
    BaseOrganizationModelManager,
    OrganizationInvitationManager,
    OrganizationMembershipManager,
)
from tenancy.slug_generation import derive_organization_slug


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

    This is the shared helper for all tenant-access checks. Call it wherever
    a view, permission, or serializer needs to resolve an active membership:

        membership = get_active_organization_membership(request.user)
        if not membership:
            return <empty queryset / clean denial>

    On the DRF request path, ``TenantScopedViewMixin.initial()``
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
    return user.memberships.filter(is_active=True).order_by("created").first()  # type: ignore[union-attr]


class ExternalEventUpdatePolicy(models.TextChoices):
    """Policy for handling inbound external provider edits and deletions.

    Controls how the app responds to edits/deletions of synced events
    coming from external providers (e.g., Google Calendar):
    - ALLOW: apply inbound edits directly (today's behavior).
    - CHANGE_REQUEST: route edits/deletions into an approval workflow.
    - FORBIDDEN: auto-undo inbound edits/deletions on the external provider.
    """

    ALLOW = "allow", "Allow direct updates"
    CHANGE_REQUEST = "change_request", "Updates create change requests"
    FORBIDDEN = "forbidden", "Updates are forbidden"


class WeekStart(models.TextChoices):
    """Day of the week that starts the week for quota period boundaries.

    Used for calculating quota periods (daily / weekly / monthly) in
    group-scoped availability rules. Does not affect recurrence rules,
    existing week handling, or any display.
    """

    MONDAY = "monday", "Monday"
    SUNDAY = "sunday", "Sunday"


class Organization(AbstractOrganization):
    """
    Represents a calendar organization.

    Extends ``vinta-django-orgs``' ``AbstractOrganization``, which supplies
    ``name``, ``slug`` (NOT NULL, unique) and ``model_utils``' ``created`` /
    ``modified``. Two things came from ``common.models.BaseModel`` before and are
    deliberately gone: the ``meta`` JSONField (verifiably unread on this model --
    only ``payments`` uses ``meta``) and the ``db_index=True`` on ``created`` /
    ``modified`` (neither is queried by timestamp range). See the plan's
    "``meta`` and timestamp indexes" Guiding Decision.
    """

    should_sync_rooms = models.BooleanField(
        default=False, help_text="Whether to sync rooms for this organization."
    )
    external_event_update_policy = models.CharField(
        max_length=20,
        choices=ExternalEventUpdatePolicy,
        default=ExternalEventUpdatePolicy.CHANGE_REQUEST,
        db_default=ExternalEventUpdatePolicy.CHANGE_REQUEST,
        help_text=(
            "Policy for handling inbound external provider edits and deletions to synced events. "
            "ALLOW: apply directly. CHANGE_REQUEST: route to approval. FORBIDDEN: auto-undo."
        ),
    )
    week_start = models.CharField(
        max_length=20,
        choices=WeekStart,
        default=WeekStart.MONDAY,
        db_default=WeekStart.MONDAY,
        help_text=(
            "Day of the week that starts the week for quota period boundaries. "
            "Used for calculating quota periods in group-scoped availability rules."
        ),
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

    class Meta(AbstractOrganization.Meta):
        # Pinned to the pre-rename table name -- the "tenancy" app label is new
        # (Phase 1a of the vinta-django-orgs migration), but no table is
        # renamed along with it. See ai-plans/2026-08-12-VINTA_DJANGO_ORGS_
        # MIGRATION_IMPLEMENTATION_PLAN.md's Guiding Decisions.
        db_table = "organizations_organization"
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["parent", "name"],
                name="uniq_org_name_per_parent",
            ),
            # Closes the empty-string loophole `Organization.save()`'s
            # `if not self.slug` derivation relies on convention alone to
            # avoid: ``slug`` is NOT NULL and unique, but nothing at the
            # database level stopped a caller that bypasses ``save()``
            # (``queryset.update(slug="")``, a historical migration model)
            # from storing ``""`` -- which satisfies NOT NULL and is
            # unique-constrained (so at most one row could ever hold it), but
            # is reachable through no supported write path and is exactly the
            # state ``tenancy.permissions.BrandingWriteGateReason.NO_SLUG``
            # used to require test helpers to manufacture out-of-band. See
            # the plan's Guiding Decisions for the "slug precondition for
            # branding writes" retirement this constraint makes permanent.
            models.CheckConstraint(
                condition=~models.Q(slug=""),
                name="organization_slug_not_blank",
            ),
        ]

    def save(self, *args, **kwargs):
        """Fill in a derived, opaque ``slug`` when the caller left one out.

        ``AbstractOrganization.slug`` is NOT NULL and unique, so "no public
        identifier yet" is no longer a storable state -- every write surface
        that used to normalize a blank slug to ``NULL`` (the REST serializer,
        the admin form, the reseller GraphQL mutation, ``baker.make``) would
        otherwise fail on the NOT NULL constraint. Deriving here rather than
        at each of those call sites keeps the invariant on the model that
        declares it.

        The derived form is the opaque ``org-<token>`` one
        (``disclose_name=False``): ``slug`` is public -- branded login URLs,
        ``brandingForTenant``, the logo delivery route -- so a name-derived
        default would make name disclosure permanent for every organization
        saved without an explicit slug. Name-derivation was only ever
        sanctioned for the Phase 1c backfill of pre-existing rows (no
        production data to disclose) and for the deliberate self-serve
        organization-create write, where a human explicitly chose the name
        for their own, about-to-be-public organization -- see
        ``OrganizationService.create_organization``, which computes and
        passes an explicit, name-derived ``slug`` itself rather than relying
        on this fallback. See the plan's Guiding Decisions.

        A slug the caller *did* supply is never touched: validation of a
        caller-supplied slug (format, reserved words, confusables, uniqueness)
        stays where it is, on the write surfaces -- see
        ``tenancy.slug_validation``.

        Only derives when the write will actually persist ``slug``:
        ``save(update_fields=[...])`` that omits ``"slug"`` skips derivation
        entirely, rather than deriving one into the in-memory instance and
        silently never writing it.
        """
        update_fields = kwargs.get("update_fields")
        if (update_fields is None or "slug" in update_fields) and not self.slug:
            taken = Organization.objects.all()
            if self.pk is not None:
                taken = taken.exclude(pk=self.pk)
            self.slug = derive_organization_slug(
                self.name,
                slug_exists=lambda candidate: taken.filter(slug=candidate).exists(),
                disclose_name=False,
            )
        super().save(*args, **kwargs)

    def is_reseller(self) -> bool:
        """Return True if this org can invite/create other organizations."""
        return self.can_invite_organizations

    def get_branding_root(self) -> Organization | None:
        """
        Resolve the organization whose branding row applies to this organization.

        Checked in this order:
        1. Walk up the parent chain for the nearest ancestor with
           ``can_invite_organizations=True`` (a reseller). If one exists, it wins --
           unchanged, and checked first, which is what preserves reseller precedence:
           a child under a reseller always resolves to the reseller, never to itself.
        2. Otherwise, if this organization itself has no parent (``parent_id is
           None``), it is its own branding root -- a parentless organization can hold
           and apply its own branding (see the write gate in
           ``tenancy.permissions``, which requires the same parentless
           condition before admitting a branding write).
        3. Otherwise (a child with no reseller ancestor), ``None`` -- it cannot brand
           itself (enforced by the write gate) and has no reseller to inherit from, so
           vinta defaults apply.
        """
        seen: set[int] = set()
        org: Organization | None = self
        while org is not None and org.pk not in seen:
            if org.can_invite_organizations:
                return org
            seen.add(org.pk)
            org = org.parent
        if self.parent_id is None:
            return self
        return None


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


class OrganizationMembership(AbstractOrganizationMembership):
    """
    Represents a membership of a user in a calendar organization.
    This is used to link users to their respective calendar organizations.

    Access rule:
        Every authenticated user is in exactly one of two states:
        1. **Has active membership** — ``get_active_organization_membership(user)``
           returns an ``OrganizationMembership`` instance and all tenant-scoped
           endpoints are open to them.
        2. **Gated (zero active memberships)** — ``get_active_organization_membership``
           returns ``None``. Only the onboarding endpoints respond:
           ``POST /organizations/`` (create own org) and ``POST /invitations/accept``
           (join an invited org). All other tenant-scoped endpoints must return an
           empty queryset or permission denial — never a 500.

        A user may hold memberships in multiple organizations.
        ``get_active_organization_membership`` resolves the *active* one: for a
        single-membership user it returns that membership; for a multi-org user it
        resolves the active org from the ``X-Organization-Id`` header.

        Never read ``user.memberships`` directly in permission / scoping code —
        always go through ``get_active_organization_membership`` so the
        resolution stays in one place.

    Extends ``vinta-django-orgs``' ``AbstractOrganizationMembership``, which
    supplies ``user`` (``related_name="memberships"``), ``organization``, the
    ``groups`` / ``permissions`` M2Ms, ``created`` / ``modified``, and the
    deliberately *unscoped* default manager. The ``groups`` / ``permissions``
    M2Ms ship empty and are read by nothing until Phase 3.
    """

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
    is_billing_owner = models.BooleanField(
        default=False,
        db_default=False,
        help_text=(
            "Whether this membership may manage the organization's billing (change "
            "plan, purchase add-ons, manage payment method) in addition to admins. "
            "This flag only marks the membership; the permission check that reads it "
            "is IsBillingOwnerOrAdmin."
        ),
    )

    # Back to a surrogate ``id``: Django cannot hang a ``ManyToManyField`` off a
    # composite-PK model, and the base class's ``groups`` / ``permissions`` are
    # exactly that. The composite PK is dropped in
    # ``0024_unwind_membership_composite_pk``; ``uniq_membership_user_organization``
    # below is NOT -- it is the unique constraint the five raw-SQL composite
    # PROTECT FKs in ``calendar_integration`` bind to (verified against
    # ``pg_constraint.conindid``), so keeping it is what makes the PK swap free
    # of any FK rebind.
    #
    # Ignored for the same reason the base class ignores its own assignment:
    # replacing an inherited manager is exactly what is intended, and mypy reads
    # a narrower manager type on a subclass as an incompatible override.
    objects = OrganizationMembershipManager()  # type: ignore[assignment,misc]

    class Meta(AbstractOrganizationMembership.Meta):
        db_table = "organizations_organizationmembership"
        # Spelled out rather than inherited so the "objects is the default
        # manager" contract is visible on the model that depends on it: the
        # reverse accessors (``user.memberships``, ``organization.memberships``)
        # are built from ``_default_manager.__class__``, and pointing them at a
        # scoped manager would empty every membership lookup that runs *before*
        # an organization has been selected.
        default_manager_name = "objects"
        # The base declares ``unique_together = [("user", "organization")]``.
        # Cleared here because the UniqueConstraint below covers exactly the same
        # columns and is the one Postgres bound the raw-SQL PROTECT FKs to;
        # keeping both would build a second, redundant unique index on the same
        # pair.
        unique_together: ClassVar[list[Sequence[str]]] = []
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
    accept). A duplicate invite to the *same* org is still rejected by the
    ``uniq_invitation_email_organization`` constraint.
    """

    objects: OrganizationInvitationManager = OrganizationInvitationManager()

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
    # rather than a real FK. Originally forced (Django 6 forbids a real FK to a
    # composite-PK model, which OrganizationMembership was until Phase 1c of the
    # vinta-django-orgs migration), and deliberately kept afterwards -- see that
    # plan's Open Questions. This contributes a
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
        db_table = "organizations_organizationinvitation"
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
    logo = S3DirectImageField(
        dest="branding_logos",
        blank=True,
        null=True,
        help_text=(
            "S3 key of the organization's uploaded logo image (PNG/JPEG/WebP; SVG rejected "
            "-- see vinta_schedule_api.settings.base.S3DIRECT_DESTINATIONS['branding_logos']). "
            "Replaces the old logo_url: the upload goes straight from the browser to our "
            "storage and only the key is stored. API reads sign the key on the way out "
            "(tenancy.branding_logo.signed_logo_url); the invitation email, whose "
            "URLs must outlive a signature, reads through the delivery route instead "
            "(tenancy.branding_logo.build_logo_delivery_url)."
        ),
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
    redirect_url = models.URLField(
        blank=True,
        default="",
        help_text=(
            "Single post-authentication redirect destination for this organization. "
            "Replaces the old return_url_allowlist: no caller-supplied redirect target "
            "is ever honored, so there is nothing to validate at request time and no "
            "open-redirect surface. Must be HTTPS with no wildcard character and no "
            "path-prefix pattern (tenancy.redirect_url_validation)."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "organizations_organizationbranding"
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

    **Deliberately ungated and, as of this phase, uncalled in production code.**
    The ``white_label_branding`` entitlement is applied by ``resolve_branding_for_display``
    instead, because not every caller of this function is presenting branding. Its only
    caller was ``public_api.queries.validate_return_url``, which read
    ``return_url_allowlist`` off the row to answer whether an OAuth return URL was
    permitted — an **auth-flow** decision, not a cosmetic one, which is why it was never
    gated: a reseller downgrading off a cosmetic entitlement must not silently break the
    OAuth return flow for every tenant underneath it. ``validate_return_url`` and the
    allowlist it read are gone (see the Organization Auth-Area Branding plan, Phase 2a),
    which leaves this function with no caller. It is kept rather than deleted here because
    Phase 5 of that plan (branding resolution) is expected to need the same ungated
    parent-walk semantics for a non-cosmetic decision; re-examine whether it still earns
    its keep once that phase lands.

    Args:
        org: The Organization instance to resolve branding for.

    Returns:
        The OrganizationBranding row of the reseller ancestor, or None if unset/no reseller.
    """
    branding_root = org.get_branding_root()
    if branding_root is None:
        return None
    return getattr(branding_root, "branding", None)


def resolve_branding_for_display(org: Organization | None) -> OrganizationBranding | None:
    """``resolve_branding``, gated on the ``white_label_branding`` entitlement.

    ``org`` may be ``None`` -- returns ``None`` immediately, at zero extra query
    cost, the same as a real, non-reseller organization with no branding-eligible
    ancestor (``get_branding_root()`` returning ``None``). This lets a caller like
    ``tenancy.views.OrganizationLogoDeliveryView._resolve_logo_key`` call
    this function unconditionally on every non-sentinel slug -- whether or not the
    slug matched a row -- instead of branching around it, which would otherwise
    make "was this function even called" an observable (query-count) difference
    between an unknown slug and an existing, unbranded organization.

    Use this for every **presentation** caller — anything that renders the reseller's
    app name, logo, colors, or support address (``branding_for_tenant``,
    ``tenancy.notification_contexts``). Use plain ``resolve_branding`` when the
    row is being read for a non-cosmetic, auth-flow decision instead — see that
    function's docstring for why it currently has no caller.

    The entitlement is resolved at the reseller's own billing root, which may differ
    from the branding root when the reseller itself pools against a grandparent — see
    ``payments.services.subscription_service.resolve_billing_root``. A reseller whose
    plan does not grant the entitlement is treated identically to one with no branding
    row: every presentation caller already falls back to the vinta default in that case
    (``branding_for_tenant``'s ``_vinta_default_branding()``, ``notification_contexts``'s
    default sender), so revoking degrades gracefully rather than erroring.

    The ``EntitlementService`` is pulled from the DI container directly (a deferred,
    function-body-local import) rather than via ``@inject``/``Provide[...]``:
    ``organizations/models.py`` carries ``from __future__ import annotations``, which
    stringifies every annotation in the module, including the ``Annotated[...,
    Provide[...]]`` marker ``@inject`` needs to introspect at wiring time. Decorating
    this function with ``@inject`` under that combination is a *silent* no-op --
    ``dependency_injector`` emits a ``DIWiringWarning`` and returns the function
    unpatched, so the parameter would always take its default and the guard below
    would never run. Precedent for the deferred-import pattern instead: the
    ``di_container`` fixture in the root ``conftest.py``, which imports ``container``
    the same way for the same reason (a module-level import would bind ``None``,
    since the container is only assigned in ``DICoreConfig.ready()`` after import
    time).

    Fails **closed** when the container itself is unavailable, matching
    ``PublicApiSystemUserMiddleware._has_partner_api_entitlement`` on the identical
    condition: an unresolvable entitlement service denies. Here that costs a reseller
    its logo until DI is repaired, which is the cheap direction to be wrong in.
    """
    if org is None:
        return None

    branding_root = org.get_branding_root()
    if branding_root is None:
        return None

    from di_core.containers import container

    if container is None:
        return None
    if not has_entitlement_cached(
        container.entitlement_service(), branding_root, Entitlement.WHITE_LABEL_BRANDING
    ):
        return None
    return getattr(branding_root, "branding", None)
