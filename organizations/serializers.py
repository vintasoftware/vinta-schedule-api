from typing import Annotated

from dependency_injector.wiring import Provide, inject
from rest_framework import serializers

from common.utils.serializer_utils import VirtualModelSerializer
from organizations.models import Organization, OrganizationInvitation
from organizations.services import OrganizationService
from organizations.virtual_models import (
    OrganizationInvitationVirtualModel,
    OrganizationVirtualModel,
)


class OrganizationSerializer(VirtualModelSerializer):
    class Meta:
        model = Organization
        virtual_model = OrganizationVirtualModel
        fields = (
            "id",
            "name",
            "should_sync_rooms",
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
