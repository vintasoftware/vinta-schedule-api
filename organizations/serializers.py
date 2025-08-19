from common.utils.serializer_utils import VirtualModelSerializer
from organizations.models import Organization
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
