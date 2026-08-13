from __future__ import annotations

from typing import Any, ClassVar

from django.conf import settings
from django.db import models

from vinta_orgs.models import AbstractOrganization, AbstractOrganizationMembership

from common.fields import OrganizationMembershipForeignKey
from common.models import BaseModel
from organizations.managers import (
    OrganizationInvitationManager,
    OrganizationMembershipManager,
)
from organizations.permission_catalog import GROUP_ORGANIZATION_MEMBER, INVITABLE_GROUPS
from organizations.slug_generation import derive_organization_slug
from payments.billing_constants import Entitlement
from payments.entitlement_cache import has_entitlement_cached
from s3direct_overrides.model_fields import S3DirectImageField


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

    Inherited from ``vinta_orgs.models.AbstractOrganization``: ``name``,
    ``slug`` (NOT NULL, unique), ``created`` and ``modified``. ``name`` is not
    redeclared -- the base already declares exactly the field we had.

    ``slug`` is inherited rather than overridden, which is what makes it NOT
    NULL. Two consequences worth knowing:

    * The column is now ``varchar(255)`` rather than ``varchar(63)``. The
      *rules* did not move: ``organizations.slug_validation`` still caps a
      written slug at ``SLUG_MAX_LENGTH`` (63) and still owns the format,
      reserved-word and confusable checks at every write surface.
    * A blank slug is refused by the database, not merely by ``save()`` -- see
      the ``organization_slug_not_blank`` constraint below. That is what
      retires ``evaluate_branding_write_gate``'s ``NO_SLUG`` condition
      permanently rather than leaving it merely hard to reach.

    ``BaseModel``'s ``meta`` JSONField and its ``db_index`` on
    ``created`` / ``modified`` are gone with the base-class change: ``meta`` was
    never read on this model and neither timestamp is queried by range.
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
        # No ``db_table``. Our app label is still ``organizations`` (the package
        # labels its own app ``vinta_orgs``), so Django's default already
        # resolves to ``organizations_organization`` -- the table this model has
        # always used. organizations/tests/test_app_identity.py pins that.
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["parent", "name"],
                name="uniq_org_name_per_parent",
            ),
            # ``slug`` is NOT NULL, but NOT NULL alone still admits ``''`` --
            # and an empty slug is not a slug: it would collide on the unique
            # index with any other blank row and it would resurrect the
            # branding gate's retired ``NO_SLUG`` state through any write that
            # goes around ``save()`` (``queryset.update(slug="")``, raw SQL, a
            # data migration). Refused in the database so no write surface has
            # to remember.
            models.CheckConstraint(
                condition=~models.Q(slug=""),
                name="organization_slug_not_blank",
            ),
        ]
        # Two of the four capability permissions from the plan's permission
        # catalog (``organizations.permission_catalog``). Named for what the
        # holder may *do*, not for the model-CRUD triples ``auth.Permission``
        # defaults to -- see that module's header.
        #
        # Declaring them here only makes ``post_migrate`` create the
        # ``auth_permission`` rows; nothing reads them until Phase 4. The
        # ``AlterModelOptions`` migration this generates emits no SQL and
        # touches no existing permission row or grant.
        permissions: ClassVar = [
            ("manage_organization", "Can manage the organization's settings"),
            ("manage_branding", "Can manage the organization's branding"),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Fill in an **opaque** slug when the caller left one out.

        ``org-<token>``, never ``slugify(name)``: the slug is public, so a
        name-derived default would publish the organization's name for every
        row saved without an explicit slug from this point on. Name derivation
        is opt-in and currently has exactly one runtime caller,
        ``OrganizationService.create_organization``, which computes and passes
        the slug itself -- see ``organizations.slug_generation``.

        Skipped entirely when ``update_fields`` is given and does not mention
        ``slug``: deriving one there would mutate the in-memory instance and
        never persist it, so the object and the row would disagree.
        """
        update_fields = kwargs.get("update_fields")
        if not self.slug and (update_fields is None or "slug" in update_fields):
            self.slug = derive_organization_slug(
                self.name,
                slug_exists=lambda candidate: (
                    type(self)._default_manager.filter(slug=candidate).exists()
                ),
                disclose_name=False,
            )
        return super().save(*args, **kwargs)

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
           ``organizations.permissions``, which requires the same parentless
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


class OrganizationMembership(AbstractOrganizationMembership):
    """
    Represents a membership of a user in a calendar organization.
    This is used to link users to their respective calendar organizations.

    Access rule:
        Every authenticated user is in exactly one of two states:
        1. **Has active membership** — the package resolver returns an
           ``OrganizationMembership`` instance and all tenant-scoped endpoints
           are open to them.
        2. **Gated (zero active memberships)** — the package resolver returns
           ``None``. Only the onboarding endpoints respond:
           ``POST /organizations/`` (create own org) and ``POST /invitations/accept``
           (join an invited org). All other tenant-scoped endpoints must return an
           empty queryset or permission denial — never a 500.

        A user may hold memberships in multiple organizations.
        ``memberships.resolve_for_user`` resolves the *active* one: for a
        single-membership user it returns that membership; for a multi-org user
        the DRF integration resolves the active org from the
        ``X-Organization-Id`` header and stores it on the request.

        Request code reads ``request.organization_membership``. Code outside a
        request calls ``common.organization_services.memberships
        .resolve_for_user`` directly so resolution stays owned by the package.

    Inherited from ``vinta_orgs.models.AbstractOrganizationMembership``:
    ``organization`` and ``user`` (both ``related_name="memberships"`` --
    the user-side accessor used to be ``memberships``), the
    ``groups`` / ``permissions`` many-to-many relations to ``auth.Group`` /
    ``auth.Permission``, and ``created`` / ``modified``.

    ``groups`` carries the three seeded groups from
    ``organizations.permission_catalog`` and is **the** representation of what a
    membership may do. It was backfilled from the two flat columns it replaced by
    migration ``0029``; those columns were dropped in Phase 6 of the
    vinta-django-orgs migration (``0030``), leaving one representation.
    ``organizations.services.assign_membership_groups`` is the single write
    path. Readers: ``organizations.auth_backends.OrganizationModelBackend``
    (which is what makes ``user.has_perm(...)`` answer per-organization),
    ``OrganizationMembershipQuerySet.holding_permission`` /
    ``billing_recipients``, and
    ``organizations.auth_backends.resolve_membership_permissions`` (the API's
    read projection). ``permissions`` -- the per-membership direct grant --
    remains unwritten, but every one of those readers unions it in, so a row
    written by hand into that table does grant the capability.

    Both M2Ms use auto-created through tables with no ``organization`` column,
    which the repo's usual rule for a many-to-many on a scoped model forbids.
    They are the package's own fields, and the exception is sound here: the
    traversal always starts from a *membership*, and a membership row is unique
    per ``(user, organization)`` -- so the organization is already pinned by the
    row the join starts from. Nothing reaches these tables from the group side.

    The primary key is a surrogate ``id`` again. The composite
    ``(user, organization)`` primary key it replaces is incompatible with a
    ``ManyToManyField``, which the two inherited relations are.
    ``uniq_membership_user_organization`` is kept -- it is a *constraint*, and
    the five raw-SQL composite PROTECT FKs in ``calendar_integration`` bind to
    it rather than to the primary key, so they survive the swap untouched.
    """

    is_active = models.BooleanField(
        default=True,
        db_default=True,
        db_index=True,
        help_text=(
            "Whether this membership is active. Inactive memberships are treated as "
            "gated: the user still has a row but loses all tenant-scoped access until "
            "reactivated. Use this to disable a user without deleting their membership "
            "record (which would lose their groups and history). Default True keeps "
            "every existing read unchanged."
        ),
    )

    # ``ClassVar`` because the base declares its own ``objects`` as one, and
    # mypy refuses to let an instance variable shadow a class variable. This
    # narrows the base's ``SingleOrganizationUnscopedManager`` to a subclass of
    # it -- the domain methods are added, the unscoped behaviour is kept.
    objects: ClassVar[OrganizationMembershipManager] = OrganizationMembershipManager()

    class Meta(AbstractOrganizationMembership.Meta):
        # No ``db_table`` -- see ``Organization.Meta``.
        #
        # ``unique_together`` is emptied deliberately. The base declares
        # ``[('user', 'organization')]``, which would build a *second* unique
        # index over the same two columns next to the
        # ``uniq_membership_user_organization`` constraint below. That
        # constraint is the one five raw-SQL composite FKs point at, so it is
        # the one that must survive verbatim; a duplicate buys nothing and
        # costs an index on every write.
        unique_together: ClassVar = []
        default_manager_name = "objects"
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["user", "organization"],
                name="uniq_membership_user_organization",
            ),
        ]
        # The membership half of the capability catalog -- see
        # ``organizations.permission_catalog`` and ``Organization.Meta`` above.
        permissions: ClassVar = [
            ("manage_members", "Can manage the organization's members"),
        ]

    def __str__(self):
        # Overrides the base, which renders every group name and therefore
        # costs a query per row anywhere a membership is stringified.
        return f"{self.user} in {self.organization}"


class OrganizationInvitation(BaseModel):
    """
    Represents an invitation to join a calendar organization.
    This is used to invite users to join their respective calendar organizations.

    The uniqueness constraint is ``unique(email, organization)`` — the same email address
    may hold concurrent pending invitations in different organizations (multi-org invite
    accept). A duplicate invite to the *same* org is still rejected by the
    ``uniq_invitation_email_organization`` constraint.

    ``group`` replaced a ``role`` column in Phase 6 of the vinta-django-orgs
    migration. It holds a *single* seeded group name rather than a list, because
    an invitation confers at most one of ``INVITABLE_GROUPS`` -- a many-to-many
    would be a table and a join for one enumerated value, and
    ``organization_billing_owner`` is refused at invitation time regardless (see
    ``organizations.permission_catalog.INVITABLE_GROUPS``).
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
    group = models.CharField(
        max_length=50,
        choices=[(name, name) for name in INVITABLE_GROUPS],
        default=GROUP_ORGANIZATION_MEMBER,
        help_text=(
            "Seeded group the membership created on acceptance joins. Defaults "
            "to 'organization_member', which confers no capability; admin "
            "invitations must name 'organization_admin' explicitly."
        ),
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    token_hash = models.TextField()
    expires_at = models.DateTimeField()
    # Membership reference via the (organization_id, membership_user_id) composite join
    # rather than a real FK. Django 6 forbids a real FK to a composite-PK model
    # (OrganizationMembership uses a composite PK). This contributes a
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
            "(organizations.branding_logo.signed_logo_url); the invitation email, whose "
            "URLs must outlive a signature, reads through the delivery route instead "
            "(organizations.branding_logo.build_logo_delivery_url)."
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
            "path-prefix pattern (organizations.redirect_url_validation)."
        ),
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
    ``organizations.views.OrganizationLogoDeliveryView._resolve_logo_key`` call
    this function unconditionally on every non-sentinel slug -- whether or not the
    slug matched a row -- instead of branching around it, which would otherwise
    make "was this function even called" an observable (query-count) difference
    between an unknown slug and an existing, unbranded organization.

    Use this for every **presentation** caller — anything that renders the reseller's
    app name, logo, colors, or support address (``branding_for_tenant``,
    ``organizations.notification_contexts``). Use plain ``resolve_branding`` when the
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
