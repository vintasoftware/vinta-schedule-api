import re
from typing import Annotated

from django.core.exceptions import ValidationError as DjangoValidationError

from dependency_injector.wiring import Provide, inject
from rest_framework import serializers

from calendar_integration.models import GoogleCalendarServiceAccount
from common.utils.serializer_utils import VirtualModelSerializer
from organizations.branding_logo import normalize_uploaded_logo_key, signed_logo_url
from organizations.exceptions import BrandingLogoUploadRejectedError
from organizations.models import (
    Organization,
    OrganizationBranding,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
)
from organizations.permissions import (
    is_branding_eligible_organization,
    is_branding_eligible_organizations,
)
from organizations.redirect_url_validation import (
    validate_redirect_url as validate_redirect_url_rule,
)
from organizations.services import OrganizationService
from organizations.slug_validation import SLUG_MAX_LENGTH, validate_organization_slug
from organizations.virtual_models import (
    OrganizationInvitationVirtualModel,
    OrganizationVirtualModel,
)
from s3direct_overrides.serializer_fields import S3DirectField


class GoogleServiceAccountWriteSerializer(serializers.Serializer):
    """Write-only nested serializer for configuring a Google Calendar service account.

    Used within OrganizationSerializer's ``google_service_account`` field.
    Accepts ``email``, ``admin_email`` and the two key fields; ``private_key``
    and ``private_key_id`` are write-only and are never echoed back in any response.
    """

    email = serializers.EmailField()
    admin_email = serializers.EmailField(allow_blank=True)
    private_key_id = serializers.CharField(write_only=True)
    private_key = serializers.CharField(write_only=True)


class GoogleServiceAccountReadSerializer(serializers.Serializer):
    """Read-only nested serializer for the Google Calendar service account status.

    Exposes only non-secret fields plus a ``configured`` boolean flag so the
    frontend can display whether credentials are set without ever returning
    ``private_key`` or ``private_key_id``.
    """

    email = serializers.CharField(read_only=True)
    admin_email = serializers.EmailField(read_only=True)
    configured = serializers.SerializerMethodField()

    def get_configured(self, obj: GoogleCalendarServiceAccount) -> bool:
        """Return True always — presence of the object means it is configured."""
        return True


class ServiceAccountReadSerializer(serializers.ModelSerializer):
    """Read serializer for the org-level Google Calendar service account (CRUD surface).

    Exposes only non-secret fields plus a ``configured`` flag. ``private_key``
    and ``private_key_id`` are never returned.
    """

    configured = serializers.SerializerMethodField()

    class Meta:
        model = GoogleCalendarServiceAccount
        fields = ("id", "email", "admin_email", "configured", "created", "modified")
        read_only_fields = fields

    def get_configured(self, obj: GoogleCalendarServiceAccount) -> bool:
        """A persisted row is, by definition, configured."""
        return True


class ServiceAccountWriteSerializer(serializers.ModelSerializer):
    """Write serializer for creating/rotating the org-level service account.

    ``private_key`` and ``private_key_id`` are write-only and are never echoed
    back in any response (reads go through ``ServiceAccountReadSerializer``).
    """

    private_key_id = serializers.CharField(max_length=255, write_only=True)
    # No max_length: a Google service-account private_key is a full PEM (~1.7KB),
    # far over 255 chars. The model stores it in an EncryptedTextField (unbounded).
    # trim_whitespace=False keeps the PEM byte-exact (its trailing newline matters
    # to some key parsers); DRF would otherwise strip it.
    private_key = serializers.CharField(write_only=True, trim_whitespace=False)

    class Meta:
        model = GoogleCalendarServiceAccount
        fields = ("email", "admin_email", "private_key_id", "private_key")


class OrganizationSerializer(VirtualModelSerializer):
    """Serializer for Organization instances.

    The ``google_service_account`` field supports both reading and writing:
    - **Write**: accepts ``email``, ``admin_email``,
      ``private_key_id`` (write-only), and ``private_key`` (write-only).
      Omitting the field on PATCH leaves existing credentials unchanged.
    - **Read**: returns ``email``, ``admin_email``, and ``configured: true/false``.
      Secret fields are never returned.
    """

    google_service_account = serializers.SerializerMethodField()

    # Explicitly declared as CharField (rather than left to ModelSerializer
    # auto-build) so we control allow_null/allow_blank/required ourselves and,
    # more importantly, so DRF does NOT auto-attach a model-derived
    # UniqueValidator: it would run before validate_slug() below, which performs
    # the uniqueness check itself with a message naming the conflicting value.
    # ``max_length`` is pinned to SLUG_MAX_LENGTH rather than inherited from the
    # model: the column is varchar(255) (inherited from the package's
    # ``AbstractOrganization``) but the rules cap a *written* slug at 63.
    # ``allow_blank`` is kept so a blank submission reaches validate_slug(),
    # which refuses it on update ("cannot be cleared") and treats it as "pick
    # one for me" on create. ``allow_null`` is False: ``slug`` is NOT NULL on
    # the model, so no response can ever carry ``null`` and no write may
    # either -- a client generated from the schema would otherwise see a
    # nullable field that 400s on ``null``.
    slug = serializers.CharField(
        required=False,
        allow_null=False,
        allow_blank=True,
        max_length=SLUG_MAX_LENGTH,
        help_text=(
            "Public, URL-safe identifier used by the organization's branded login page and by brandingForTenant. Always set: an organization saved without one is given an opaque ``org-<token>`` slug, and the database refuses a blank value. Mutable, but not clearable -- changing it orphans previously-issued branded login URLs, which then fall back to the default identity rather than erroring. Format, reserved-word, and confusable-character rules live in organizations.slug_validation and are enforced by each write surface (REST serializer, admin form, GraphQL input), not on the model."
        ),
    )

    # ``get_google_service_account`` issues exactly one bounded, org-scoped query
    # through the tenant manager (the org-level GoogleCalendarServiceAccount,
    # ``calendar_fk__isnull=True``). It is not prefetched: the query is bounded and
    # this serializer only ever serializes one Organization, so the manager lookup
    # is the sanctioned tenant access path. This serializer only
    # ever serializes a single Organization (retrieve / current / update — there
    # is no list endpoint), so the extra query is bounded at 1. Without this the
    # VirtualModelSerializer's zero-query budget raises QueryCountExceededException
    # on every read under DEBUG.
    max_queries_count = 1

    class Meta:
        model = Organization
        virtual_model = OrganizationVirtualModel
        read_only_fields = (
            "id",
            "created",
            "can_invite_organizations",
            "modified",
        )
        fields = (
            "id",
            "name",
            "slug",
            "should_sync_rooms",
            "external_event_update_policy",
            "week_start",
            "google_service_account",
            "can_invite_organizations",
            "created",
            "modified",
        )

    def get_google_service_account(self, obj: Organization) -> dict | None:
        """Return read-only service account info (no secrets), or None if unconfigured."""
        account = (
            GoogleCalendarServiceAccount.objects.filter_by_organization(obj.id)
            .filter(calendar_fk__isnull=True)
            .first()
        )
        if account is None:
            return None
        return GoogleServiceAccountReadSerializer(account).data

    def validate_slug(self, value: str) -> str:
        """Validate format/reserved-word/confusable rules, then uniqueness.

        A blank slug on **update** is refused: ``slug`` is NOT NULL and the
        ``organization_slug_not_blank`` check constraint rejects ``""``, so
        there is nothing sensible to write. It is refused rather than ignored
        because silently ignoring it would let a client believe it had cleared
        the organization's public identifier — and, since ``Organization.save()``
        mints a replacement for an empty slug, the row would end up with a
        *different* slug rather than the one the client can see. Omitting the
        field entirely still means "leave it alone". ``null`` is rejected by
        the field itself (``allow_null=False``) before this method ever runs.

        A blank slug on **create** is accepted and means "pick one for me":
        ``OrganizationService.create_organization`` derives it from the name
        -- ``create()`` below never passes ``slug`` through, so the value
        returned here for that case is unused.

        Uniqueness is checked here, against the shared queryset, excluding the
        instance being updated, so a collision returns 400 naming the conflicting
        value rather than a 500 from the DB's unique-index integrity error.
        """
        if not value:
            if self.instance is not None:
                raise serializers.ValidationError(
                    "Slug cannot be cleared once set. Send a new slug, or omit the "
                    "field to leave it unchanged."
                )
            return value

        try:
            validate_organization_slug(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc

        queryset = Organization.objects.filter(slug=value)
        if self.instance is not None:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError(
                f"An organization with the slug '{value}' already exists."
            )
        return value

    @inject
    def __init__(
        self,
        *args,
        organization_service: Annotated[OrganizationService, Provide["organization_service"]],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.organization_service = organization_service

    def create(self, validated_data):
        creator = self.context["request"].user
        organization = self.organization_service.create_organization(
            creator=creator,
            name=validated_data["name"],
            should_sync_rooms=validated_data.get("should_sync_rooms", False),
            external_event_update_policy=validated_data.get("external_event_update_policy"),
            week_start=validated_data.get("week_start"),
        )
        return organization


class OrganizationInvitationSerializer(VirtualModelSerializer):
    """
    Serializer for managing OrganizationInvitation instances.
    """

    class Meta:
        model = OrganizationInvitation
        virtual_model = OrganizationInvitationVirtualModel
        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "organization",
            "invited_by",
            "accepted_at",
            "expires_at",
            "created",
            "modified",
        )
        read_only_fields = (
            "id",
            "organization",
            "invited_by",
            "accepted_at",
            "expires_at",
            "created",
            "modified",
        )

    @inject
    def __init__(
        self,
        *args,
        organization_service: Annotated[OrganizationService, Provide["organization_service"]],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.organization_service = organization_service

    def validate_email(self, value: str) -> str:
        """Validate that email is properly formatted and not already invited."""
        # Check if there's already a pending invitation for this email in this organization
        organization = self.context["organization"]

        existing_member = organization.memberships.filter(user__email__iexact=value).first()
        if existing_member:
            raise serializers.ValidationError(
                "This email is already associated with a member of the organization."
            )

        return value

    def create(self, validated_data: dict) -> OrganizationInvitation:
        """Create invitation by calling the service method."""
        organization = self.context["organization"]
        invited_by = self.context["request"].user

        invitation = self.organization_service.invite_user_to_organization(
            email=validated_data["email"],
            first_name=validated_data["first_name"],
            last_name=validated_data["last_name"],
            invited_by=invited_by,
            organization=organization,
        )

        return invitation


class CurrentMembershipSerializer(serializers.ModelSerializer):
    """Read-only serializer for the caller's current organization membership.

    Returns the membership role and the nested organization so the frontend
    can distinguish between an onboarded user and a gated (membership-less) user.
    """

    organization = serializers.SerializerMethodField()
    can_manage_branding = serializers.SerializerMethodField()

    class Meta:
        model = OrganizationMembership
        fields = ("role", "organization", "can_manage_branding")
        read_only_fields = ("role", "organization", "can_manage_branding")

    def get_organization(self, obj: OrganizationMembership) -> dict:
        """Serialize the related organization using OrganizationSerializer."""
        return OrganizationSerializer(obj.organization, context=self.context).data  # type: ignore[call-arg]

    def get_can_manage_branding(self, obj: OrganizationMembership) -> bool:
        """Whether the membership's organization is branding-eligible.

        Computed as parentless-and-entitled. It once deliberately excluded a
        third, slug condition (Organization Auth-Area Branding plan, Phase 4
        Capability signal guiding decision) so an organization missing only a
        slug still saw the branding page; that condition is now retired, so the
        exclusion is vacuous and the two checks coincide. Shares
        ``organizations.permissions.is_branding_eligible_organization`` rather
        than restating the two-condition check, so this tracks the same gate
        that governs ``GET /branding/`` (see
        ``OrganizationBrandingView._check_branding_read_gate``) rather than
        the write gate.
        """
        return is_branding_eligible_organization(obj.organization)


class OrganizationBriefSerializer(serializers.ModelSerializer):
    """Lightweight read-only serializer for an Organization.

    Exposes only the fields needed for the org-switcher list: ``id``, ``name``,
    and the read-only ``slug`` (so the frontend can render/link the branded login
    URL without a second request). Intentionally avoids the heavier
    ``OrganizationSerializer`` (which loads the Google service account) to keep
    ``GET /organizations/mine/`` fast.
    """

    class Meta:
        model = Organization
        fields = ("id", "name", "slug")
        read_only_fields = ("id", "name", "slug")
        # ``slug`` is inherited from the package's ``AbstractOrganization``,
        # which declares no ``help_text`` -- so without this the OpenAPI schema
        # would describe the organization's public identifier with nothing at
        # all. Kept in step with ``OrganizationSerializer.slug`` above.
        extra_kwargs = {  # noqa: RUF012
            "slug": {
                "help_text": (
                    "Public, URL-safe identifier used by the organization's branded login page and by brandingForTenant. Always set: an organization saved without one is given an opaque ``org-<token>`` slug, and the database refuses a blank value. Mutable, but not clearable -- changing it orphans previously-issued branded login URLs, which then fall back to the default identity rather than erroring. Format, reserved-word, and confusable-character rules live in organizations.slug_validation and are enforced by each write surface (REST serializer, admin form, GraphQL input), not on the model."
                )
            }
        }


class _MyMembershipListSerializer(serializers.ListSerializer):
    """Batches ``can_manage_branding`` for the whole membership list up front.

    ``GET /organizations/mine/`` lists every active membership for the caller,
    which can span N distinct organizations. Left to
    ``MyMembershipSerializer.get_can_manage_branding`` calling
    ``is_branding_eligible_organization`` per row, that would be N entitlement
    lookups (see ``EntitlementService.has_entitlement_for_organizations`` for
    what each one costs). This resolves the whole batch with
    ``is_branding_eligible_organizations`` once, in two queries total, and
    stashes the result on the shared serializer context so each child's
    ``get_can_manage_branding`` reads from it instead of asking individually.
    """

    def to_representation(self, data):
        iterable = data.all() if hasattr(data, "all") else data
        memberships = list(iterable)
        self.context["_can_manage_branding_by_organization_pk"] = (
            is_branding_eligible_organizations(
                [membership.organization for membership in memberships]
            )
        )
        return super().to_representation(memberships)


class MyMembershipSerializer(serializers.ModelSerializer):
    """Read-only serializer for the caller's active organization memberships.

    Used by ``GET /organizations/mine/`` to power the frontend org switcher.
    Returns a list of ``{organization: {id, name}, role, can_manage_branding}``
    entries — one per active membership — without requiring the
    ``X-Organization-Id`` header.
    """

    organization = OrganizationBriefSerializer(read_only=True)
    can_manage_branding = serializers.SerializerMethodField()

    class Meta:
        model = OrganizationMembership
        fields = ("organization", "role", "can_manage_branding")
        read_only_fields = ("organization", "role", "can_manage_branding")
        list_serializer_class = _MyMembershipListSerializer

    def get_can_manage_branding(self, obj: OrganizationMembership) -> bool:
        """Whether this membership's organization is branding-eligible
        (parentless-and-entitled) -- see
        ``CurrentMembershipSerializer.get_can_manage_branding`` for the full
        rationale. Computed per-membership (not per-role): a non-admin
        member's entry reports the same organization-level capability as an
        admin's, matching the read gate's own admin-agnostic eligibility
        check -- role-based write authorization is enforced separately by
        ``IsOrganizationAdmin`` on the branding endpoints themselves.

        Reads from the batch ``_MyMembershipListSerializer`` precomputes on the
        shared context when serializing a list (the ``many=True`` path this
        serializer is actually used on). Falls back to the single-organization
        ``is_branding_eligible_organization`` call when there is no such batch
        in context (e.g. this serializer instantiated directly against one
        membership), which is exactly what the batch entry would have computed
        for that one organization anyway.
        """
        batch = self.context.get("_can_manage_branding_by_organization_pk")
        if batch is not None:
            return batch.get(obj.organization_id, False)
        return is_branding_eligible_organization(obj.organization)


class OrganizationMembershipSerializer(serializers.ModelSerializer):
    """
    Read-only serializer for listing and retrieving organization members.

    Exposes membership role, active status, and flattened user information
    (email, first_name, last_name) for the admin datatable.
    """

    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_first_name = serializers.CharField(source="user.profile.first_name", read_only=True)
    user_last_name = serializers.CharField(source="user.profile.last_name", read_only=True)

    class Meta:
        model = OrganizationMembership
        # OrganizationMembership has a composite PK (user, organization) and no scalar
        # ``id``. Expose the membership identity as ``user_id`` + ``organization_id``
        # (Open Question #1 resolution: a membership is identified by the (user, org)
        # pair) instead of the dropped ``id``.
        fields = (
            "user_id",
            "organization_id",
            "role",
            "is_active",
            "user_email",
            "user_first_name",
            "user_last_name",
        )
        read_only_fields = fields


class UpdateMembershipRoleSerializer(serializers.Serializer):
    """Request serializer for updating an organization member's role."""

    role = serializers.ChoiceField(choices=OrganizationRole.choices)


class AcceptInvitationSerializer(serializers.Serializer):
    """
    Serializer for accepting invitations via public endpoint.
    """

    token = serializers.CharField(required=True)

    @inject
    def __init__(
        self,
        *args,
        organization_service: Annotated[OrganizationService, Provide["organization_service"]],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.organization_service = organization_service

    def create(self, validated_data: dict):
        """Accept invitation by calling the service method."""
        user = self.context["request"].user
        token = validated_data["token"]

        return self.organization_service.accept_invitation(token=token, user=user)


def _validate_hex_color(value: str) -> str:
    """Validate a hex color string: #RRGGBB or #RRGGBBAA. Returns the value unchanged."""
    if value and not re.match(r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$", value):
        raise serializers.ValidationError("Invalid color format. Expected #RRGGBB or #RRGGBBAA.")
    return value


class BrandingLogoField(S3DirectField):
    """The project's standard S3-direct field, narrowed to the branding-logo
    upload destination.

    Read and write behavior both come from ``s3direct_overrides.serializer_fields.
    S3DirectField``, the same field every other S3-backed column serializes
    through: reads return a **time-limited signed URL** for the stored key,
    writes accept either a bare key or a full signed/public URL and persist only
    the key. Signing is what keeps a re-uploaded logo from being pinned in a
    browser/CDN cache — the URL changes with the key — which the delivery
    route's stable per-slug URL could not do.

    ``source="logo"`` binds this to the model's ``logo`` (``S3DirectImageField``)
    column while keeping the serializer's field name ``logo_url`` stable.

    The two overrides below are the branding-specific narrowing, and are the
    reason this is a subclass rather than a bare ``S3DirectField``:

    - writes run the normalized key through ``normalize_uploaded_logo_key``,
      which rejects anything outside ``BRANDING_LOGO_KEY_PREFIX`` — without it
      an admin could point ``logo`` at another destination's object in the
      shared media bucket (``providers_documents/``, which holds PHI) and read
      back a signed URL for it;
    - reads go through ``signed_logo_url``, which applies the same prefix guard
      on the way out for a row written around this serializer.

    Unlike the delivery route, this field does **not** substitute our default
    logo for a missing one: ``None`` is the honest answer for "this organization
    has not uploaded a logo", and it is what lets the SPA's branding settings
    form render an empty upload widget. The public read surfaces that must
    always render something use ``branding_logo.build_logo_display_url``.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("allow_blank", True)
        # Reads return null for a logoless row, so the published schema has to
        # say so. It also gives clients a second way to clear the logo (null,
        # alongside ""), which the model's ``null=True`` column stores as-is.
        kwargs.setdefault("allow_null", True)
        super().__init__(*args, **kwargs)

    def to_internal_value(self, data: str) -> str:
        value = super().to_internal_value(data)
        try:
            return normalize_uploaded_logo_key(value)
        except BrandingLogoUploadRejectedError as e:
            raise serializers.ValidationError(str(e)) from e

    # ``CharField.to_representation`` is stubbed as returning ``str``, but DRF
    # serializes a ``None`` return as JSON null perfectly well -- which is the
    # honest representation of a row with no logo, and what the published schema
    # declares (``nullable: true``).
    def to_representation(self, value) -> str | None:  # type: ignore[override]
        return signed_logo_url(value)


class OrganizationBrandingSerializer(serializers.ModelSerializer):
    """Serializer for OrganizationBranding (reseller-admin REST endpoints).

    Exposes app_name, logo_url, primary_color, secondary_color, support_email,
    and redirect_url. NEVER exposes can_invite_organizations or makes
    organization writable (the org is set from the acting org in the view).

    Validates:
    - Color format: #RRGGBB or #RRGGBBAA (regex).
    - redirect_url: HTTPS scheme, no wildcard character, no path-prefix pattern
      (organizations.redirect_url_validation).

    ``logo_url`` round-trips through ``BrandingLogoField``: reads return a
    time-limited signed URL for the stored object (``None`` when no logo is
    set), writes accept the uploaded S3 key from the ``branding_logos``
    S3Direct destination.
    """

    logo_url = BrandingLogoField(source="logo")

    class Meta:
        model = OrganizationBranding
        fields = (
            "app_name",
            "logo_url",
            "primary_color",
            "secondary_color",
            "support_email",
            "redirect_url",
        )

    def validate_primary_color(self, value: str) -> str:
        """Validate primary_color hex format: #RRGGBB or #RRGGBBAA."""
        return _validate_hex_color(value)

    def validate_secondary_color(self, value: str) -> str:
        """Validate secondary_color hex format: #RRGGBB or #RRGGBBAA."""
        return _validate_hex_color(value)

    def validate_redirect_url(self, value: str) -> str:
        """Validate redirect_url: HTTPS scheme, no wildcard, no path-prefix pattern."""
        try:
            validate_redirect_url_rule(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return value


class OrganizationBrandingLogoUploadParamsRequestSerializer(serializers.Serializer):
    """Request body for ``OrganizationBrandingLogoUploadParamsView``. Mirrors
    ``users.serializers.ProfilePictureUploadParamsRequestSerializer``."""

    file_name = serializers.CharField()
    file_type = serializers.CharField()
    file_size = serializers.IntegerField(min_value=1)


class OrganizationBrandingLogoUploadParamsSerializer(serializers.Serializer):
    """Response body for ``OrganizationBrandingLogoUploadParamsView`` — the same
    shape ``organizations.branding_logo.sign_branding_logo_upload`` and the
    GraphQL ``create_branding_logo_upload`` mutation return.

    ``upload_url`` is a complete SigV4 presigned PUT URL. The client uploads by
    PUTting the file body straight to it with a matching Content-Type and no
    other headers — no AWS credentials reach the browser and nothing has to be
    signed client-side. Mirrors ``users.serializers.ProfilePictureUploadParamsSerializer``.
    """

    object_key = serializers.CharField()
    upload_url = serializers.URLField()
    expires_in = serializers.IntegerField()
