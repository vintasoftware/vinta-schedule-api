from rest_framework.permissions import IsAuthenticated

from common.utils.view_utils import NoListVintaScheduleModelViewSet
from organizations.models import Organization
from organizations.permissions import OrganizationManagementPermission
from organizations.serializers import OrganizationSerializer


class OrganizationViewSet(NoListVintaScheduleModelViewSet):
    """
    A viewset for managing organizations.
    """

    queryset = Organization.objects.all()
    serializer_class = OrganizationSerializer
    permission_classes = (IsAuthenticated, OrganizationManagementPermission)

    def get_queryset(self):
        user = self.request.user
        if hasattr(user, "organization_membership") and user.organization_membership:
            return Organization.objects.filter(id=user.organization_membership.organization_id)
        return Organization.objects.none()
