from typing import Annotated

from dependency_injector.wiring import Provide, inject
from rest_framework import serializers

from calendar_integration.models import GoogleCalendarServiceAccount
from common.utils.serializer_utils import VirtualModelSerializer
from organizations.models import Organization, OrganizationInvitation, OrganizationMembership
from organizations.services import OrganizationService
from organizations.virtual_models import (
    OrganizationInvitationVirtualModel,
    OrganizationVirtualModel,
)


class GoogleServiceAccountWriteSerializer(serializers.Serializer):
    """Write-only nested serializer for configuring a Google Calendar service account.

    Used within OrganizationSerializer's ``google_service_account`` field.
    Accepts all five credential fields; ``private_key`` and ``private_key_id``
    are write-only and are never echoed back in any response.
    """

    email = serializers.EmailField()
    audience = serializers.CharField(max_length=255, allow_blank=True)
    public_key = serializers.CharField()
    private_key_id = serializers.CharField(write_only=True)
    private_key = serializers.CharField(write_only=True)


class GoogleServiceAccountReadSerializer(serializers.Serializer):
    """Read-only nested serializer for the Google Calendar service account status.

    Exposes only non-secret fields plus a ``configured`` boolean flag so the
    frontend can display whether credentials are set without ever returning
    ``private_key`` or ``private_key_id``.
    """

    email = serializers.CharField(read_only=True)
    audience = serializers.CharField(read_only=True)
    configured = serializers.SerializerMethodField()

    def get_configured(self, obj: GoogleCalendarServiceAccount) -> bool:
        """Return True always — presence of the object means it is configured."""
        return True


class ServiceAccountReadSerializer(serializers.ModelSerializer):
    """Read serializer for the org-level Google Calendar service account (CRUD surface).

    Exposes only non-secret fields plus a ``configured`` flag. ``public_key``,
    ``private_key`` and ``private_key_id`` are never returned.
    """

    configured = serializers.SerializerMethodField()

    class Meta:
        model = GoogleCalendarServiceAccount
        fields = ("id", "email", "audience", "configured", "created", "modified")
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
        fields = ("email", "audience", "public_key", "private_key_id", "private_key")


class OrganizationSerializer(VirtualModelSerializer):
    """Serializer for Organization instances.

    The ``google_service_account`` field supports both reading and writing:
    - **Write**: accepts ``email``, ``audience``, ``public_key``,
      ``private_key_id`` (write-only), and ``private_key`` (write-only).
      Omitting the field on PATCH leaves existing credentials unchanged.
    - **Read**: returns ``email``, ``audience``, and ``configured: true/false``.
      Secret fields are never returned.
    """

    google_service_account = serializers.SerializerMethodField()

    # ``get_google_service_account`` issues exactly one bounded, org-scoped query
    # through the tenant manager (the org-level GoogleCalendarServiceAccount,
    # ``calendar_fk__isnull=True``). It can't be prefetched: OrganizationModel's
    # ``organization`` FK uses ``related_name="+"`` (no reverse accessor), so the
    # manager lookup is the sanctioned tenant access path. This serializer only
    # ever serializes a single Organization (retrieve / current / update — there
    # is no list endpoint), so the extra query is bounded at 1. Without this the
    # VirtualModelSerializer's zero-query budget raises QueryCountExceededException
    # on every read under DEBUG.
    max_queries_count = 1

    class Meta:
        model = Organization
        virtual_model = OrganizationVirtualModel
        fields = (
            "id",
            "name",
            "should_sync_rooms",
            "google_service_account",
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

    class Meta:
        model = OrganizationMembership
        fields = ("role", "organization")
        read_only_fields = ("role", "organization")

    def get_organization(self, obj: OrganizationMembership) -> dict:
        """Serialize the related organization using OrganizationSerializer."""
        return OrganizationSerializer(obj.organization, context=self.context).data  # type: ignore[call-arg]


class OrganizationBriefSerializer(serializers.ModelSerializer):
    """Lightweight read-only serializer for an Organization.

    Exposes only the fields needed for the org-switcher list: ``id`` and ``name``.
    Intentionally avoids the heavier ``OrganizationSerializer`` (which loads the
    Google service account) to keep ``GET /organizations/mine/`` fast.
    """

    class Meta:
        model = Organization
        fields = ("id", "name")
        read_only_fields = ("id", "name")


class MyMembershipSerializer(serializers.ModelSerializer):
    """Read-only serializer for the caller's active organization memberships.

    Used by ``GET /organizations/mine/`` to power the frontend org switcher.
    Returns a list of ``{organization: {id, name}, role}`` entries — one per
    active membership — without requiring the ``X-Organization-Id`` header.
    """

    organization = OrganizationBriefSerializer(read_only=True)

    class Meta:
        model = OrganizationMembership
        fields = ("organization", "role")
        read_only_fields = ("organization", "role")


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
        fields = (
            "id",
            "role",
            "is_active",
            "user_email",
            "user_first_name",
            "user_last_name",
        )
        read_only_fields = fields


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
