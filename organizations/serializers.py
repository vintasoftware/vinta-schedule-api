from typing import Annotated

from dependency_injector.wiring import Provide, inject

from common.utils.serializer_utils import VirtualModelSerializer
from organizations.models import Organization
from organizations.services import OrganizationService
from organizations.virtual_models import OrganizationVirtualModel


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
